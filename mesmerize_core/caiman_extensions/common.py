import os
from functools import partial, wraps
from pathlib import Path
from subprocess import Popen
from typing import Union, List, Optional
from uuid import UUID, uuid4

import numpy as np
import pandas as pd

from ..batch_utils import (
    COMPUTE_BACKENDS,
    COMPUTE_BACKEND_SUBPROCESS,
    ALGO_MODULES,
    get_parent_raw_data_path,
    PathsDataFrameExtension,
    PathsSeriesExtension,
    HAS_PYQT,
)
from ..utils import validate_path, IS_WINDOWS, make_runfile

if HAS_PYQT:
    from PyQt5 import QtCore


def validate(algo: str = None):
    def dec(func):
        @wraps(func)
        def wrapper(self, *args, **kwargs):
            if self._series["outputs"] is None:
                raise ValueError("Item has not been run")

            if algo is not None:
                if algo not in self._series["algo"]:
                    raise ValueError(
                        f"<{algo} extension called for a <{self._series}> item"
                    )

            if not self._series["outputs"]["success"]:
                raise ValueError("Cannot load output of an unsuccessful item")
            return func(self, *args, **kwargs)

        return wrapper

    return dec


@pd.api.extensions.register_dataframe_accessor("caiman")
class CaimanDataFrameExtensions:
    """
    Extensions for caiman related functions
    """

    def __init__(self, df: pd.DataFrame):
        self._df = df

    def uloc(self, u: Union[str, UUID]) -> pd.Series:
        """
        Return the series corresponding to the passed UUID
        """
        df_u = self._df.loc[self._df["uuid"] == str(u)]

        if df_u.index.size == 0:
            raise KeyError("Item with given UUID not found in dataframe")
        elif df_u.index.size > 1:
            raise KeyError(
                f"Duplicate items with given UUID found in dataframe, something is wrong\n"
                f"{df_u}"
            )

        return df_u.squeeze()

    def add_item(self, algo: str, name: str, input_movie_path: str, params: dict):
        """
        Add an item to the DataFrame to organize parameters
        that can be used to run a CaImAn algorithm

        Parameters
        ----------
        algo: str
            Name of the algorithm to run, see `ALGO_MODULES` dict

        name: str
            User set name for the batch item

        input_movie_path: str
            Full path to the input movie

        params:
            Parameters for running the algorithm with the input movie

        """
        # make sure path is within batch dir or parent raw data path
        input_movie_path = self._df.paths.resolve(input_movie_path)
        validate_path(input_movie_path)

        # get relative path
        input_movie_path = self._df.paths.split(input_movie_path)[1]

        # Create a pandas Series (Row) with the provided arguments
        s = pd.Series(
            {
                "algo": algo,
                "name": name,
                "input_movie_path": str(input_movie_path),
                "params": params,
                "outputs": None,  # to store dict of output information, such as output file paths
                "uuid": str(
                    uuid4()
                ),  # unique identifier for this combination of movie + params
            }
        )

        # Add the Series to the DataFrame
        self._df.loc[self._df.index.size] = s

        # Save DataFrame to disk
        self._df.to_pickle(self._df.paths.get_batch_path())

    def remove_item(self, index):
        # Drop selected index
        self._df.drop([index], inplace=True)
        # Reset indeces so there are no 'jumps'
        self._df.reset_index(drop=True, inplace=True)
        # Save new df to disc
        self._df.to_pickle(self._df.paths.get_batch_path())


@pd.api.extensions.register_series_accessor("caiman")
class CaimanSeriesExtensions:
    """
    Extensions for caiman stuff
    """

    def __init__(self, s: pd.Series):
        self._series = s
        self.process: [Union, QtCore.QProcess, Popen] = None

    def _run_qprocess(
        self,
        runfile_path: str,
        callbacks_finished: List[callable],
        callback_std_out: Optional[callable] = None,
    ) -> QtCore.QProcess:

        # Create a QProcess
        self.process = QtCore.QProcess()
        self.process.setProcessChannelMode(QtCore.QProcess.MergedChannels)

        # Set the callback function to read the stdout
        if callback_std_out is not None:
            self.process.readyReadStandardOutput.connect(
                partial(callback_std_out, self.process)
            )

        # connect the callback functions for when the process finishes
        if callbacks_finished is not None:
            for f in callbacks_finished:
                self.process.finished.connect(f)

        parent_path = self._series.paths.resolve(self._series.input_movei_path).parent

        # Set working dir for the external process
        self.process.setWorkingDirectory(str(parent_path))

        # Start the external process
        if IS_WINDOWS:
            self.process.start("powershell.exe", [runfile_path])
        else:
            self.process.start(runfile_path)

        return self.process

    def _run_subprocess(
        self,
        runfile_path: str,
        callbacks_finished: List[callable] = None,
        callback_std_out: Optional[callable] = None,
    ):

        # Get the dir that contains the input movie
        parent_path = self._series.paths.resolve(self._series.input_movie_path).parent

        self.process = Popen(runfile_path, cwd=parent_path)
        return self.process

    def _run_slurm(
        self,
        runfile_path: str,
        callbacks_finished: List[callable],
        callback_std_out: Optional[callable] = None,
    ):
        submission_command = (
            f'sbatch --ntasks=1 --cpus-per-task=16 --mem=90000 --wrap="{runfile_path}"'
        )

        Popen(submission_command.split(" "))

    def run(
        self,
        backend: Optional[str] = COMPUTE_BACKEND_SUBPROCESS,
        callbacks_finished: Optional[List[callable]] = None,
        callback_std_out: Optional[callable] = None,
    ):
        """
        Run a CaImAn algorithm in an external process using the chosen backend

        NoRMCorre, CNMF, or CNMFE will be run for this Series.
        Each Series (DataFrame row) has a `input_movie_path` and `params` for the algorithm

        Parameters
        ----------
        backend: Optional[str]
            One of the available backends, if none default is `COMPUTE_BACKEND_SUBPROCESS`

        callbacks_finished: List[callable]
            List of callback functions that are called when the external process has finished.

        callback_std_out: Optional[callable]
            callback function to pipe the stdout
        """
        if backend not in COMPUTE_BACKENDS:
            raise KeyError(
                f"Invalid or unavailable `backend`, choose from the following backends:\n"
                f"{COMPUTE_BACKENDS}"
            )

        batch_path = self._series.paths.get_batch_path()

        # Create the runfile in the batch dir using this Series' UUID as the filename
        runfile_path = str(
            batch_path.parent.joinpath(self._series["uuid"] + ".runfile")
        )

        args_str = f"--batch-path {batch_path} --uuid {self._series.uuid}"
        if get_parent_raw_data_path() is not None:
            args_str += f" --data-path {get_parent_raw_data_path()}"

        # make the runfile
        runfile = make_runfile(
            module_path=os.path.abspath(
                ALGO_MODULES[self._series["algo"]].__file__
            ),  # caiman algorithm
            filename=runfile_path,  # path to create runfile
            args_str=args_str,
        )
        try:
            self.process = getattr(self, f"_run_{backend}")(
                runfile, callbacks_finished, callback_std_out
            )
        except:
            with open(runfile_path, "r") as f:
                raise ValueError(f.read())

        return self.process

    @validate()
    def get_input_movie_path(self) -> Path:
        """
        Returns
        -------
        Path
            full path to the input movie file
        """

        return self._series.paths.resolve(self._series["input_movie_path"])

    @validate()
    def get_correlation_image(self) -> np.ndarray:
        """
        Returns
        -------
        np.ndarray
            correlation image
        """
        path = self._series.paths.resolve(self._series["outputs"]["corr-img-path"])
        return np.load(str(path))

    @validate()
    def get_pnr_image(self) -> np.ndarray:
        """
        Returns
        -------
        np.ndarray
            pnr image
        """
        path = self._series.paths.resolve(self._series["outputs"]["pnr-image-path"])
        return np.load(str(path))

    @validate()
    def get_projection(self, proj_type: str) -> np.ndarray:
        path = self._series.paths.resolve(
            self._series["outputs"][f"{proj_type}-projection-path"]
        )
        return np.load(path)

    # TODO: finish the copy_data() extension
    # def copy_data(self, new_parent_dir: Union[Path, str]):
    #     """
    #     Copy all data associated with this series to a different parent dir
    #     """
    #     movie_path = get_full_raw_data_path(self._series['input_movie_path'])
    #     output_paths = []
    #     for p in self._series['outputs']
