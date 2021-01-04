# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.

import numpy as np
from pycuda import gpuarray
import pycuda.driver as cuda

import svirl.config as cfg
from svirl.storage import GArray

class Grid(object):
    """This class contains methods to retrieve the grid information 
    at cell centroids, horizontal and vertical edge mid-points, 
    vertices/nodes as well as material tiling. 
    """

    def __init__(self, par):
        self.par = par
        self.__set_flags_krnl = self.par.get_function('set_flags')
        self.__clear_flags_krnl = self.par.get_function('clear_flags')

        self._mt = None
        self.__have_material_tiling = False

        self._flags = GArray(shape = (cfg.Nx, cfg.Ny), dtype = np.int32)
        self.material_tiling = cfg.material_tiling


    def __del__(self):
        self.__free_mt()


    def have_material_tiling(self):
        return self.__have_material_tiling


#--- grids ---#

    @property
    def xy(self):
        """Coordinates of grid vertices"""
        return (np.linspace(0.0, cfg.Lx, num=cfg.Nx, endpoint=True, dtype=cfg.dtype), 
                np.linspace(0.0, cfg.Ly, num=cfg.Ny, endpoint=True, dtype=cfg.dtype))


    @property
    def xy_grid(self):
        """Coordinates of grid vertices, Nc-by-Ny grid"""
        x, y = self.xy
        return np.meshgrid(x, y, indexing='ij')


    @property
    def xy_a(self):
        """Coordinates of horizontal edge centers"""
        return (np.linspace(0.5*cfg.dx, cfg.Lx-0.5*cfg.dx, num=cfg.Nxa, endpoint=True, dtype=cfg.dtype), 
                np.linspace(0.0,        cfg.Ly,            num=cfg.Nya, endpoint=True, dtype=cfg.dtype))


    @property
    def xy_a_grid(self):
        """Coordinates of horizontal edge centers, Nxa-by-Nya grid"""
        x, y = self.xy_a
        return np.meshgrid(x, y, indexing='ij')


    @property
    def xy_b(self):
        """Coordinates of vertical edge centers"""
        return (np.linspace(0.0,        cfg.Lx,            num=cfg.Nxb, endpoint=True, dtype=cfg.dtype), 
                np.linspace(0.5*cfg.dy, cfg.Ly-0.5*cfg.dy, num=cfg.Nyb, endpoint=True, dtype=cfg.dtype))


    @property
    def xy_b_grid(self):
        """Coordinates of vertical edge centers, Nxb-by-Nyb grid"""
        x, y = self.xy_b
        return np.meshgrid(x, y, indexing='ij')


    @property
    def xy_c(self):
        """Coordinates of cell centers"""
        return (np.linspace(0.5*cfg.dx, cfg.Lx-0.5*cfg.dx, num=cfg.Nxc, endpoint=True, dtype=cfg.dtype), 
                np.linspace(0.5*cfg.dy, cfg.Ly-0.5*cfg.dy, num=cfg.Nyc, endpoint=True, dtype=cfg.dtype))


    @property
    def xy_c_grid(self):
        """Coordinates of cell centers, Nxc-by-Nyc grid"""
        x, y = self.xy_c
        return np.meshgrid(x, y, indexing='ij')


    def __free_mt(self):
        if self._mt is not None:
            self._mt.free()
            self._mt = None
            self.__have_material_tiling = False


    @property
    def material_tiling(self):
        self._flags.sync()
        return self._flags.get_h().copy().astype(np.bool)


    @material_tiling.setter
    def material_tiling(self, material_tiling):

        if callable(material_tiling):
            xg, yg = self.xy_c_grid
            mt = material_tiling(xg, yg)
        else:
            mt = material_tiling

        # create or delete material_tiling
        if mt is not None:
            assert mt.shape == (cfg.Nxc, cfg.Nyc)

            self._mt = GArray(like = mt.astype(np.bool))
            self.__have_material_tiling = True
        else: 
            self.__free_mt()
            self._clear_flags()

        # set link variable computation flags based on material tiling
        self._set_flags()

        # _flags replaces material tiling; free _mt
        self.__free_mt()


    def material_tiling_h(self):
        if self._mt is not None:
            return self._mt.get_d_obj()

        return np.uintp(0)


    def _flags_h(self):
        return self._flags.get_d_obj()


    def __in_material(self, x, y, prohibited_length=None):
        # TODO: test in_material() method
        if isinstance(x, (np.floating, float, np.integer, int)) and isinstance(y, (np.floating, float, np.integer, int)):
            xs, ys, scalar = np.array([x], dtype=self.dtype), np.array([y], dtype=self.dtype), True
        else:
            xs, ys, scalar = x, y, False
        assert xs.shape == ys.shape
        if prohibited_length is None: prohibited_length = self.dtype(0.0)
        
        in_mt = np.full_like(xs, True, dtype=np.bool)
        
        in_mt[np.logical_or.reduce((
            xs < prohibited_length, 
            xs > self.Lx-prohibited_length,
            ys < prohibited_length,
            ys > self.Ly-prohibited_length,
        ))] = False
        
        xg_c, yg_c = self.xy_c_grid
        xg_c, yg_c = self.flatten_c_array(xg_c), self.flatten_c_array(yg_c)
        
        in_mt = in_mt.reshape(-1)
        for e, (x_, y_) in enumerate(zip(xs.ravel(), ys.ravel())):
            if np.any(np.logical_and(np.square(xg_c-x_) + np.square(yg_c-y_) < np.square(prohibited_length), ~self.mt)):
                im_mt[e] = False
        in_mt = in_mt.reshape(xs.shape)
        
        return in_mt if not scalar else in_mt[0]


    def _get_material_tiling_at_nodes(self):
        mt = self._mt.get_h()

        mt_c = np.full((1, cfg.Nyc), False, dtype=np.bool)
        mt_r = np.full((cfg.Nx, 1), False, dtype=np.bool)
        
        return (np.logical_or.reduce((
            np.c_[mt_r, np.r_[mt_c, mt]],
            np.c_[mt_r, np.r_[mt, mt_c]],
            np.c_[np.r_[mt_c, mt], mt_r],
            np.c_[np.r_[mt, mt_c], mt_r]
        )))


    def interpolate_ab_array_to_c_array(self, a, b):
        cx = 0.5*(a[:, :-1] + a[:, 1:])
        cy = 0.5*(b[:-1, :] + b[1:, :])
        return cx, cy


    def interpolate_ab_array_to_c_array_abs(self, a, b):
        cx, cy = self.interpolate_ab_array_to_c_array(a, b)
        return np.sqrt(np.square(cx) + np.square(cy))


    def _set_flags(self):
        self.__set_flags_krnl(
                self.material_tiling_h(), 
                self._flags_h(),

                grid  = (self.par.grid_size, 1, 1),
                block = (self.par.block_size, 1, 1), 
                )

        self._flags.need_dtoh_sync()
        self._flags.sync()


    def _clear_flags(self):
        self.__clear_flags_krnl(
                self.material_tiling_h(), 
                self._flags_h(),

                grid  = (self.par.grid_size, 1, 1),
                block = (self.par.block_size, 1, 1), 
                )

        self._flags.need_dtoh_sync()
        self._flags.sync()
