from functools import lru_cache
from pathlib import Path
from typing import *

import numpy
import numpy as np
import pandas as pd
from caiman import load_memmap
from caiman.source_extraction.cnmf import CNMF
from caiman.source_extraction.cnmf.cnmf import load_CNMF
from caiman.utils.visualization import get_contours as caiman_get_contours

from .common import validate


@pd.api.extensions.register_series_accessor("cnmf")
class CNMFExtensions:
    """
    Extensions for managing CNMF output data
    """

    def __init__(self, s: pd.Series):
        self._series = s

    def get_cnmf_memmap(self) -> np.ndarray:
        """
        Get the CNMF memmap

        Returns
        -------
        np.ndarray
            numpy memmap array used for CNMF
        """
        path = self._series.paths.resolve(self._series["outputs"]["cnmf-memmap-path"])
        # Get order f images
        Yr, dims, T = load_memmap(str(path))
        images = np.reshape(Yr.T, [T] + list(dims), order="F")
        return images

    def get_input_memmap(self) -> np.ndarray:
        """
        Return the F-order memmap if the input to the
        CNMF batch item was a mcorr output memmap

        Returns
        -------
        np.ndarray
            numpy memmap array of the input
        """
        movie_path = str(self._series.caiman.get_input_movie_path())
        if movie_path.endswith("mmap"):
            Yr, dims, T = load_memmap(movie_path)
            images = np.reshape(Yr.T, [T] + list(dims), order="F")
            return images
        else:
            raise TypeError(
                f"Input movie for CNMF was not a memmap, path to input movie is:\n"
                f"{movie_path}"
            )

    # TODO: Cache this globally so that a common upper cache limit is valid for ALL batch items
    @validate("cnmf")
    def get_output_path(self) -> Path:
        """
        Returns
        -------
        Path
            Path to the Caiman CNMF hdf5 output file
        """
        return self._series.paths.resolve(self._series["outputs"]["cnmf-hdf5-path"])

    @validate("cnmf")
    def get_output(self) -> CNMF:
        """
        Returns
        -------
        CNMF
            Returns the Caiman CNMF object
        """
        # Need to create a cache object that takes the item's UUID and returns based on that
        # collective global cache
        return load_CNMF(self.get_output_path())

    # TODO: Make the ``ixs`` parameter for spatial stuff optional
    @validate("cnmf")
    def get_spatial_masks(
        self, ixs_components: Optional[np.ndarray] = None, threshold: float = 0.01
    ) -> np.ndarray:
        """
        Get binary masks of the spatial components at the given `ixs`

        Basically created from cnmf.estimates.A

        Parameters
        ----------
        ixs_components: np.ndarray
            numpy array containing integer indices for which you want spatial masks.
            if `None` uses cnmf.estimates.idx_components

        threshold: float
            threshold

        Returns
        -------
        np.ndarray
            shape is [dim_0, dim_1, n_components]

        """
        cnmf_obj = self.get_output()

        dims = cnmf_obj.dims
        if dims is None:
            dims = cnmf_obj.estimates.dims

        if ixs_components is None:
            ixs_components = cnmf_obj.estimates.idx_components

        masks = np.zeros(shape=(dims[0], dims[1], len(ixs_components)), dtype=bool)

        for n, ix in enumerate(ixs_components):
            s = cnmf_obj.estimates.A[:, ix].toarray().reshape(cnmf_obj.dims)
            s[s >= threshold] = 1
            s[s < threshold] = 0

            masks[:, :, n] = s.astype(bool)

        return masks

    # TODO: Cache this globally so that a common upper cache limit is valid for ALL batch items
    @staticmethod
    def _get_spatial_contours(
        cnmf_obj: CNMF, ixs_components: Optional[np.ndarray] = None
    ):
        if ixs_components is None:
            ixs_components = cnmf_obj.estimates.idx_components

        dims = cnmf_obj.dims
        if dims is None:
            # I think that one of these is `None` if loaded from an hdf5 file
            dims = cnmf_obj.estimates.dims

        # need to transpose these
        dims = dims[1], dims[0]

        contours = caiman_get_contours(
            cnmf_obj.estimates.A[:, ixs_components], dims, swap_dim=True
        )

        return contours

    @validate("cnmf")
    def get_spatial_contours(
        self, ixs_components: Optional[np.ndarray] = None
    ) -> Tuple[List[np.ndarray], List[np.ndarray]]:
        """
        Get the contour and center of mass for each spatial footprint

        Parameters
        ----------
        ixs_components: np.ndarray
            indices for which to return spatial contours.
            if `None` uses cnmf.estimates.idx_components

        Returns
        -------

        """
        cnmf_obj = self.get_output()
        contours = self._get_spatial_contours(cnmf_obj, ixs_components)

        coordinates = list()
        coms = list()

        for contour in contours:
            coors = contour["coordinates"]
            coors = coors[~np.isnan(coors).any(axis=1)]
            coordinates.append(coors)

            com = coors.mean(axis=0)
            coms.append(com)

        return coordinates, coms

    @validate("cnmf")
    def get_temporal_components(
        self, ixs_components: Optional[np.ndarray] = None, add_background: bool = False
    ) -> np.ndarray:
        """
        Get the temporal components for this CNMF item

        Parameters
        ----------
        ixs_components: np.ndarray
            indices for which to return temporal components, ``cnmf.estimates.C``.
            if `None` uses cnmf.estimates.idx_components

        add_background: bool
            if ``True``, add the temporal background, basically ``cnmf.estimates.C + cnmf.estimates.f``

        Returns
        -------

        """
        cnmf_obj = self.get_output()

        if ixs_components is None:
            ixs_components = cnmf_obj.estimates.idx_components

        C = cnmf_obj.estimates.C[ixs_components]
        f = cnmf_obj.estimates.f

        if add_background:
            return C + f
        else:
            return C

    # TODO: Cache this globally so that a common upper cache limit is valid for ALL batch items
    @validate("cnmf")
    def get_reconstructed_movie(
        self,
        ixs_frames: Optional[Union[Tuple[int, int], int]] = None,
        idx_components: np.ndarray = None,
        add_background: bool = True,
    ) -> np.ndarray:
        """
        Return the reconstructed movie, (A * C) + (b * f)

        Parameters
        ----------
        ixs_frames: Tuple[int, int], int
            (start_frame, stop_frame), return frames in this range including the ``start_frame``, upto and not
            including the ``stop_frame``
            if single int, return reconstructed movie for single frame indicated

        add_background: bool
            if ``True``, add the spatial & temporal background, b * f

        Returns
        -------
        np.ndarray
            shape is [n_frames, x_pixels, y_pixels]
        """
        cnmf_obj = self.get_output()

        if idx_components is None:
            idx_components = np.arange(cnmf_obj.estimates.A.shape[1])

        if ixs_frames is None:
            ixs_frames = (0, cnmf_obj.estimates.C.shape[1])

        if isinstance(ixs_frames, int):
            ixs_frames = (ixs_frames, ixs_frames + 1)

        dn = cnmf_obj.estimates.A[:,idx_components].dot(
            cnmf_obj.estimates.C[idx_components, ixs_frames[0] : ixs_frames[1]]
        )

        if add_background:
            dn += cnmf_obj.estimates.b.dot(
                cnmf_obj.estimates.f[:, ixs_frames[0] : ixs_frames[1]]
            )
        return dn.reshape(cnmf_obj.dims + (-1,), order="F").transpose([2, 0, 1])

    @validate("cnmf")
    def get_residuals(
        self,
        ixs_frames: Optional[Union[Tuple[int, int], int]] = None,
    ) -> np.ndarray:
        """
        Return the residuals of a given frame, movie - (A * C)

        Parameters
        ----------
        Tuple[int, int], int
            (start_frame, stop_frame), return residuals for frames in this range including the ``start_frame``, upto and not
            including the ``stop_frame``
            if single int, return residual for single frame indicated

        Returns
        -------
        np.ndarray
            shape is [n_frames, x_pixels, y_pixels]
        """

        if ixs_frames is None:
            ixs_frames = (0, self.get_input_memmap().shape[0])

        if isinstance(ixs_frames, int):
            ixs_frames = (ixs_frames, ixs_frames + 1)

        raw_movie = self.get_input_memmap()

        reconstructed_movie = self.get_reconstructed_movie(ixs_frames, True)

        residuals = raw_movie[np.arange(*ixs_frames)] - reconstructed_movie

        return residuals







