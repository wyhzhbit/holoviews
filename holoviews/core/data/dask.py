from __future__ import absolute_import

try:
    import itertools.izip as zip
except ImportError:
    pass

import numpy as np
import pandas as pd
import dask.dataframe as dd
from dask.dataframe import DataFrame
from dask.dataframe.core import Scalar

from .. import util
from ..element import Element
from ..ndmapping import NdMapping, item_check
from .interface import Interface
from .pandas import PandasInterface


class DaskInterface(PandasInterface):

    types = (DataFrame,)

    datatype = 'dask'

    default_partitions = 100

    @classmethod
    def init(cls, eltype, data, kdims, vdims):
        data, kdims, vdims = PandasInterface.init(eltype, data, kdims, vdims)
        if not isinstance(data, DataFrame):
            data = dd.from_pandas(data, npartitions=cls.default_partitions, sort=False)
        return data, kdims, vdims

    @classmethod
    def shape(cls, dataset):
        return (len(dataset.data), len(dataset.data.columns))

    @classmethod
    def range(cls, columns, dimension):
        column = columns.data[columns.get_dimension(dimension).name]
        if column.dtype.kind == 'O':
            column = np.sort(column[column.notnull()].compute())
            return column[0], column[-1]
        else:
            return (column.min().compute(), column.max().compute())

    @classmethod
    def sort(cls, columns, by=[]):
        columns.warning('Dask dataframes do not support sorting')
        return columns.data

    @classmethod
    def values(cls, columns, dim, expanded=True, flat=True):
        data = columns.data[dim]
        if not expanded:
            data = data.unique()
        return data.compute().values

    @classmethod
    def select_mask(cls, dataset, selection):
        """
        Given a Dataset object and a dictionary with dimension keys and
        selection keys (i.e tuple ranges, slices, sets, lists or literals)
        return a boolean mask over the rows in the Dataset object that
        have been selected.
        """
        select_mask = None
        for dim, k in selection.items():
            if isinstance(k, tuple):
                k = slice(*k)
            masks = []
            series = dataset.data[dim]
            if isinstance(k, slice):
                if k.start is not None:
                    masks.append(k.start <= series)
                if k.stop is not None:
                    masks.append(series < k.stop)
            elif isinstance(k, (set, list)):
                iter_slc = None
                for ik in k:
                    mask = series == ik
                    if iter_slc is None:
                        iter_slc = mask
                    else:
                        iter_slc |= mask
                masks.append(iter_slc)
            elif callable(k):
                masks.append(k(series))
            else:
                masks.append(series == k)
            for mask in masks:
                if select_mask:
                    select_mask &= mask
                else:
                    select_mask = mask
        return select_mask

    @classmethod
    def select(cls, columns, selection_mask=None, **selection):
        df = columns.data
        if selection_mask is not None:
            print selection_mask
            return df[selection_mask]
        selection_mask = cls.select_mask(columns, selection)
        indexed = cls.indexed(columns, selection)
        df = df if selection_mask is None else df[selection_mask]
        if indexed and len(df) == 1:
            return df[columns.vdims[0].name].compute().iloc[0]
        return df
    
    @classmethod
    def groupby(cls, columns, dimensions, container_type, group_type, **kwargs):
        index_dims = [columns.get_dimension(d) for d in dimensions]
        element_dims = [kdim for kdim in columns.kdims
                        if kdim not in index_dims]

        group_kwargs = {}
        if group_type != 'raw' and issubclass(group_type, Element):
            group_kwargs = dict(util.get_param_values(columns),
                                kdims=element_dims)
        group_kwargs.update(kwargs)

        groupby = columns.data.groupby(dimensions)
        indices = columns.data[dimensions].compute().values
        coords = list(util.unique_iterator((tuple(ind) for ind in indices)))
        data = []
        for coord in coords:
            if any(isinstance(c, float) and np.isnan(c) for c in coord):
                continue
            if len(coord) == 1:
                coord = coord[0]
            group = group_type(groupby.get_group(coord), **group_kwargs)
            data.append((coord, group))
        if issubclass(container_type, NdMapping):
            with item_check(False):
                return container_type(data, kdims=index_dims)
        else:
            return container_type(data)
    
    @classmethod
    def aggregate(cls, columns, dimensions, function, **kwargs):
        data = columns.data
        cols = [d.name for d in columns.kdims if d in dimensions]
        vdims = columns.dimensions('value', True)
        dtypes = data.dtypes
        numeric = [c for c, dtype in zip(dtypes.index, dtypes.values)
                   if dtype.kind in 'iufc' and c in vdims]
        reindexed = data[cols+numeric]

        inbuilts = {'amin': 'min', 'amax': 'max', 'mean': 'mean',
                    'std': 'std', 'sum': 'sum', 'var': 'var'}
        if len(dimensions):
            groups = reindexed.groupby(cols, sort=False)
            if (hasattr(function, 'func_name') and function.func_name in inbuilts):
                agg = getattr(groups, inbuilts[function.func_name])()
            else:
                agg = groups.apply(function, axis=1)
            return agg.reset_index()
        else:
            if (hasattr(function, 'func_name') and function.func_name in inbuilts):
                agg = getattr(reindexed, inbuilts[function.func_name])()
            else:
                raise NotImplementedError
            return pd.DataFrame(agg.compute()).T

    @classmethod
    def unpack_scalar(cls, columns, data):
        """
        Given a columns object and data in the appropriate format for
        the interface, return a simple scalar.
        """
        if len(data.columns) > 1 or len(data) != 1:
            return data
        if isinstance(data, dd.DataFrame):
            data = data.compute()
        return data.iat[0,0]

    @classmethod
    def sample(cls, columns, samples=[]):
        data = columns.data
        dims = columns.dimensions('key', label=True)
        mask = None
        for sample in samples:
            if np.isscalar(sample): sample = [sample]
            for i, (c, v) in enumerate(zip(dims, sample)):
                dim_mask = data[c]==v
                if mask is None:
                    mask = dim_mask
                else:
                    mask |= dim_mask
        return data[mask]

    @classmethod
    def add_dimension(cls, columns, dimension, dim_pos, values, vdim):
        data = columns.data
        if dimension.name not in data.columns:
            if not np.isscalar(values):
                err = 'Dask dataframe does not support assigning non-scalar value.'
                raise NotImplementedError(err)
            data = data.assign(**{dimension.name: values})
        return data

    @classmethod
    def concat(cls, columns_objs):
        cast_objs = cls.cast(columns_objs)
        return dd.concat([col.data for col in cast_objs])

    @classmethod
    def dframe(cls, columns, dimensions):
        return columns.data.compute()


Interface.register(DaskInterface)
