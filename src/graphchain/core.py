"""Graphchain core."""

import datetime as dt
import logging
import time
from copy import deepcopy
from functools import cached_property, lru_cache, partial
from pickle import HIGHEST_PROTOCOL  # noqa: S403
from typing import Any, Callable, Container, Dict, Hashable, Iterable, Optional, Union

import cloudpickle
import dask
import fs
import fs.base
import fs.memoryfs
import fs.osfs
import joblib
from dask.highlevelgraph import HighLevelGraph, Layer

from .utils import get_size, str_to_posix_fully_portable_filename


def hlg_setitem(self: HighLevelGraph, key: Hashable, value: Any) -> None:
    """Set a HighLevelGraph computation."""
    for d in self.layers.values():
        if key in d:
            d[key] = value  # type: ignore[index]
            break


# Monkey patch HighLevelGraph to add a missing `__setitem__` method.
if not hasattr(HighLevelGraph, "__setitem__"):
    HighLevelGraph.__setitem__ = hlg_setitem  # type: ignore[index]


def layer_setitem(self: Layer, key: Hashable, value: Any) -> None:
    """Set a Layer computation."""
    self.mapping[key] = value  # type: ignore[attr-defined]


# Monkey patch Layer to add a missing `__setitem__` method.
if not hasattr(Layer, "__setitem__"):
    Layer.__setitem__ = layer_setitem  # type: ignore[index]


logger = logging.getLogger(__name__)


def joblib_dump(
    obj: Any, fs: fs.base.FS, key: str, ext: str = "joblib", **kwargs: Dict[str, Any]
) -> None:
    """Store an object on a filesystem."""
    filename = f"{key}.{ext}"
    with fs.open(filename, "wb") as fid:
        joblib.dump(obj, fid, **kwargs)


def joblib_load(fs: fs.base.FS, key: str, ext: str = "joblib", **kwargs: Dict[str, Any]) -> Any:
    """Load an object from a filesystem."""
    filename = f"{key}.{ext}"
    with fs.open(filename, "rb") as fid:
        return joblib.load(fid, **kwargs)


joblib_dump_lz4 = partial(joblib_dump, compress="lz4", ext="joblib.lz4", protocol=HIGHEST_PROTOCOL)
joblib_load_lz4 = partial(joblib_load, ext="joblib.lz4")


class CacheFS:
    """Lazily opened PyFileSystem."""

    def __init__(self, location: Union[str, fs.base.FS]):
        """Wrap a PyFilesystem FS URL to provide a single FS instance.

        Parameters
        ----------
        location
            A PyFilesystem FS URL to store the cached computations in. Can be a local directory such
            as ``"./__graphchain_cache__/"`` or a remote directory such as
            ``"s3://bucket/__graphchain_cache__/"``. You can also pass a PyFilesystem itself
            instead.
        """
        self.location = location

    @cached_property
    def fs(self) -> fs.base.FS:
        """Open a PyFilesystem FS to the cache directory."""
        # create=True does not yet work for S3FS [1]. This should probably be left to the user as we
        # don't know in which region to create the bucket, among other configuration options.
        # [1] https://github.com/PyFilesystem/s3fs/issues/23
        if isinstance(self.location, fs.base.FS):
            return self.location
        return fs.open_fs(self.location, create=True)


class CachedComputation:
    """A replacement for computations in dask graphs."""

    def __init__(
        self,
        dsk: Dict[Hashable, Any],
        key: Hashable,
        computation: Any,
        location: Union[str, fs.base.FS, CacheFS] = "./__graphchain_cache__/",
        serialize: Callable[[Any, fs.base.FS, str], None] = joblib_dump_lz4,
        deserialize: Callable[[fs.base.FS, str], Any] = joblib_load_lz4,
        write_to_cache: Union[bool, str] = "auto",
    ) -> None:
        """Cache a dask graph computation.

        A wrapper for the computation object to replace the original computation with in the dask
        graph.

        Parameters
        ----------
        dsk
            The dask graph this computation is a part of.
        key
            The key corresponding to this computation in the dask graph.
        computation
            The computation to cache.
        location
            A PyFilesystem FS URL to store the cached computations in. Can be a local directory such
            as ``"./__graphchain_cache__/"`` or a remote directory such as
            ``"s3://bucket/__graphchain_cache__/"``. You can also pass a CacheFS instance or a
            PyFilesystem itself instead.
        serialize
            A function of the form ``serialize(result: Any, fs: fs.base.FS, key: str)`` that caches
            a computation `result` to a filesystem `fs` under a given `key`.
        deserialize
            A function of the form ``deserialize(fs: fs.base.FS, key: str)`` that reads a cached
            computation `result` from a `key` on a given filesystem `fs`.
        write_to_cache
            Whether or not to cache this computation. If set to ``"auto"``, will only write to cache
            if it is expected this will speed up future gets of this computation, taking into
            account the characteristics of the `location` filesystem.
        """
        self.dsk = dsk
        self.key = key
        self.computation = computation
        self.location = location
        self.serialize = serialize
        self.deserialize = deserialize
        self.write_to_cache = write_to_cache

    @cached_property
    def cache_fs(self) -> fs.base.FS:
        """Open a PyFilesystem FS to the cache directory."""
        # create=True does not yet work for S3FS [1]. This should probably be left to the user as we
        # don't know in which region to create the bucket, among other configuration options.
        # [1] https://github.com/PyFilesystem/s3fs/issues/23
        if isinstance(self.location, fs.base.FS):
            return self.location
        if isinstance(self.location, CacheFS):
            return self.location.fs
        return fs.open_fs(self.location, create=True)

    def __repr__(self) -> str:
        """Represent this CachedComputation object as a string."""
        return f"<CachedComputation key={self.key} task={self.computation} hash={self.hash}>"

    def _subs_dependencies_with_hash(self, computation: Any) -> Any:
        """Replace key references in a computation by their hashes."""
        dependencies = dask.core.get_dependencies(
            self.dsk, task=0 if computation is None else computation
        )
        for dep in dependencies:
            computation = dask.core.subs(
                computation,
                dep,
                self.dsk[dep].hash
                if isinstance(self.dsk[dep], CachedComputation)
                else self.dsk[dep][0].hash,
            )
        return computation

    def _subs_tasks_with_src(self, computation: Any) -> Any:
        """Replace task functions by their source code."""
        if type(computation) is list:
            # This computation is a list of computations.
            computation = [self._subs_tasks_with_src(x) for x in computation]
        elif dask.core.istask(computation):
            # This computation is a task.
            src = joblib.func_inspect.get_func_code(computation[0])[0]
            computation = (src,) + computation[1:]
        return computation

    def compute_hash(self) -> str:
        """Compute a hash of this computation object and its dependencies."""
        # Replace dependencies with their hashes and functions with source.
        computation = self._subs_dependencies_with_hash(self.computation)
        computation = self._subs_tasks_with_src(computation)
        # Return the hash of the resulting computation.
        comp_hash: str = joblib.hash(cloudpickle.dumps(computation))
        return comp_hash

    @property
    def hash(self) -> str:
        """Return the hash of this CachedComputation."""
        if not hasattr(self, "_hash"):
            self._hash = self.compute_hash()
        return self._hash

    def estimate_load_time(self, result: Any) -> float:
        """Estimate the time to load the given result from cache."""
        size: float = get_size(result) / dask.config.get("cache_estimated_compression_ratio", 2.0)
        # Use typical SSD latency and bandwith if cache_fs is a local filesystem, else use a typical
        # latency and bandwidth for network-based filesystems.
        read_latency = float(
            dask.config.get(
                "cache_latency",
                1e-4 if isinstance(self.cache_fs, (fs.osfs.OSFS, fs.memoryfs.MemoryFS)) else 50e-3,
            )
        )
        read_throughput = float(
            dask.config.get(
                "cache_throughput",
                500e6 if isinstance(self.cache_fs, (fs.osfs.OSFS, fs.memoryfs.MemoryFS)) else 50e6,
            )
        )
        return read_latency + size / read_throughput

    @lru_cache  # noqa: B019
    def read_time(self, timing_type: str) -> float:
        """Read the time to load, compute, or store from file."""
        time_filename = f"{self.hash}.time.{timing_type}"
        with self.cache_fs.open(time_filename, "r") as fid:
            return float(fid.read())

    def write_time(self, timing_type: str, seconds: float) -> None:
        """Write the time to load, compute, or store from file."""
        time_filename = f"{self.hash}.time.{timing_type}"
        with self.cache_fs.open(time_filename, "w") as fid:
            fid.write(str(seconds))

    def write_log(self, log_type: str) -> None:
        """Write the timestamp of a load, compute, or store operation."""
        key = str_to_posix_fully_portable_filename(str(self.key))
        now = str_to_posix_fully_portable_filename(str(dt.datetime.now()))
        log_filename = f".{now}.{log_type}.{key}.log"
        with self.cache_fs.open(log_filename, "w") as fid:
            fid.write(self.hash)

    def time_to_result(self, memoize: bool = True) -> float:
        """Estimate the time to load or compute this computation."""
        if hasattr(self, "_time_to_result"):
            return self._time_to_result  # type: ignore[has-type,no-any-return]
        if memoize:
            try:
                try:
                    load_time = self.read_time("load")
                except Exception:
                    load_time = self.read_time("store") / 2
                self._time_to_result = load_time
                return load_time
            except Exception:  # noqa: S110
                pass
        compute_time = self.read_time("compute")
        dependency_time = 0
        dependencies = dask.core.get_dependencies(
            self.dsk, task=0 if self.computation is None else self.computation
        )
        for dep in dependencies:
            dependency_time += self.dsk[dep][0].time_to_result()
        total_time = compute_time + dependency_time
        if memoize:
            self._time_to_result = total_time
        return total_time

    def cache_file_exists(self) -> bool:
        """Check if this CachedComputation's cache file exists."""
        return self.cache_fs.exists(f"{self.hash}.time.store")

    def load(self) -> Any:
        """Load this result of this computation from cache."""
        try:
            # Load from cache.
            start_time = time.perf_counter()
            logger.info(f"LOAD {self} from {self.cache_fs}/{self.hash}")
            result = self.deserialize(self.cache_fs, self.hash)
            load_time = time.perf_counter() - start_time
            # Write load time and log operation.
            self.write_time("load", load_time)
            self.write_log("load")
            return result
        except Exception:
            logger.exception(
                f"Could not read {self.cache_fs}/{self.hash}. Marking cache as invalid, please try again!"
            )
            self.cache_fs.remove(f"{self.hash}.time.store")
            raise

    def compute(self, *args: Any, **kwargs: Any) -> Any:
        """Compute this computation."""
        # Compute the computation.
        logger.info(f"COMPUTE {self}")
        start_time = time.perf_counter()
        if dask.core.istask(self.computation):
            result = self.computation[0](*args, **kwargs)
        else:
            result = args[0]
        compute_time = time.perf_counter() - start_time
        # Write compute time and log operation
        self.write_time("compute", compute_time)
        self.write_log("compute")
        return result

    def store(self, result: Any) -> None:
        """Store the result of this computation in the cache."""
        if not self.cache_file_exists():
            logger.info(f"STORE {self} to {self.cache_fs}/{self.hash}")
            try:
                # Store to cache.
                start_time = time.perf_counter()
                self.serialize(result, self.cache_fs, self.hash)
                store_time = time.perf_counter() - start_time
                # Write store time and log operation
                self.write_time("store", store_time)
                self.write_log("store")
            except Exception:
                # Not crucial to stop if caching fails.
                logger.exception(f"Could not write {self.hash}.")

    def patch_computation_in_graph(self) -> None:
        """Patch the graph to use this CachedComputation."""
        if self.cache_file_exists():
            # If there are cache candidates to load this computation from, remove all dependencies
            # for this task from the graph as far as dask is concerned.
            self.dsk[self.key] = (self,)
        else:
            # If there are no cache candidates, wrap the execution of the computation with this
            # CachedComputation's __call__ method and keep references to its dependencies.
            self.dsk[self.key] = (
                (self,) + self.computation[1:]
                if dask.core.istask(self.computation)
                else (self, self.computation)
            )

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        """Load this computation from cache, or compute and then store it."""
        # Load.
        if self.cache_file_exists():
            return self.load()
        # Compute.
        result = self.compute(*args, **kwargs)
        # Store.
        write_to_cache = self.write_to_cache
        if write_to_cache == "auto":
            compute_time = self.time_to_result(memoize=False)
            estimated_load_time = self.estimate_load_time(result)
            write_to_cache = estimated_load_time < compute_time
            logger.debug(
                f'{"Going" if write_to_cache else "Not going"} to cache {self}'
                f" because estimated_load_time={estimated_load_time} "
                f'{"<" if write_to_cache else ">="} '
                f"compute_time={compute_time}"
            )
        if write_to_cache:
            self.store(result)
        return result


def optimize(
    dsk: Dict[Hashable, Any],
    keys: Optional[Union[Hashable, Iterable[Hashable]]] = None,
    skip_keys: Optional[Container[Hashable]] = None,
    location: Union[str, fs.base.FS, CacheFS] = "./__graphchain_cache__/",
    serialize: Callable[[Any, fs.base.FS, str], None] = joblib_dump_lz4,
    deserialize: Callable[[fs.base.FS, str], Any] = joblib_load_lz4,
) -> Dict[Hashable, Any]:
    """Optimize a dask graph with cached computations.

    According to the dask graph specification [1]_, a dask graph is a dictionary that maps `keys` to
    `computations`. A computation can be:

        1. Another key in the graph.
        2. A literal.
        3. A task, which is of the form `(Callable, *args)`.
        4. A list of other computations.

    This optimizer replaces all computations in a graph with ``CachedComputation``'s, so that
    getting items from the graph will be backed by a cache of your choosing. With this cache, only
    the very minimum number of computations will actually be computed to return the values
    corresponding to the given keys.

    `CachedComputation` objects *do not* hash task inputs (which is the approach that
    `functools.lru_cache` and `joblib.Memory` take) to identify which cache file to load. Instead, a
    chain of hashes (hence the name `graphchain`) of the computation object and its dependencies
    (which are also computation objects) is used to identify the cache file.

    Since it is generally cheap to hash the graph's computation objects, `graphchain`'s cache is
    likely to be much faster than hashing task inputs, which can be slow for large objects such as
    `pandas.DataFrame`'s.

    Parameters
    ----------
    dsk
        The dask graph to optimize with caching computations.
    keys
        Not used. Is present for compatibility with dask optimizers [2]_.
    skip_keys
        A container of keys not to cache.
    location
        A PyFilesystem FS URL to store the cached computations in. Can be a local directory such as
        ``"./__graphchain_cache__/"`` or a remote directory such as
        ``"s3://bucket/__graphchain_cache__/"``. You can also pass a PyFilesystem itself instead.
    serialize
        A function of the form ``serialize(result: Any, fs: fs.base.FS, key: str)`` that caches a
        computation `result` to a filesystem `fs` under a given `key`.
    deserialize
        A function of the form ``deserialize(fs: fs.base.FS, key: str)`` that reads a cached
        computation `result` from a `key` on a given filesystem `fs`.

    Returns
    -------
    dict
        A copy of the dask graph where the computations have been replaced by `CachedComputation`'s.

    References
    ----------
    .. [1] https://docs.dask.org/en/latest/spec.html
    .. [2] https://docs.dask.org/en/latest/optimize.html
    """
    # Verify that the graph is a DAG.
    dsk = deepcopy(dsk)
    assert dask.core.isdag(dsk, list(dsk.keys()))
    if isinstance(location, str):
        location = CacheFS(location)
    # Replace graph computations by CachedComputations.
    skip_keys = skip_keys or set()
    for key, computation in dsk.items():
        dsk[key] = CachedComputation(
            dsk,
            key,
            computation,
            location=location,
            serialize=serialize,
            deserialize=deserialize,
            write_to_cache=False if key in skip_keys else "auto",
        )
    # Remove task arguments if we can load from cache.
    for key in dsk:
        dsk[key].patch_computation_in_graph()
    return dsk


def get(
    dsk: Dict[Hashable, Any],
    keys: Union[Hashable, Iterable[Hashable]],
    skip_keys: Optional[Container[Hashable]] = None,
    location: Union[str, fs.base.FS, CacheFS] = "./__graphchain_cache__/",
    serialize: Callable[[Any, fs.base.FS, str], None] = joblib_dump_lz4,
    deserialize: Callable[[fs.base.FS, str], Any] = joblib_load_lz4,
    scheduler: Optional[
        Callable[[Dict[Hashable, Any], Union[Hashable, Iterable[Hashable]]], Any]
    ] = None,
) -> Any:
    """Get one or more keys from a dask graph with caching.

    Optimizes a dask graph with `graphchain.optimize` and then computes the requested keys with the
    desired scheduler, which is by default `dask.get`.

    See `graphchain.optimize` for more information on how `graphchain`'s cache mechanism works.

    Parameters
    ----------
    dsk
        The dask graph to query.
    keys
        The keys to compute.
    skip_keys
        A container of keys not to cache.
    location
        A PyFilesystem FS URL to store the cached computations in. Can be a local directory such as
        ``"./__graphchain_cache__/"`` or a remote directory such as
        ``"s3://bucket/__graphchain_cache__/"``. You can also pass a PyFilesystem itself instead.
    serialize
        A function of the form ``serialize(result: Any, fs: fs.base.FS, key: str)`` that caches a
        computation `result` to a filesystem `fs` under a given `key`.
    deserialize
        A function of the form ``deserialize(fs: fs.base.FS, key: str)`` that reads a cached
        computation `result` from a `key` on a given filesystem `fs`.
    scheduler
        The dask scheduler to use to retrieve the keys from the graph.

    Returns
    -------
    Any
        The computed values corresponding to the given keys.
    """
    cached_dsk = optimize(
        dsk,
        keys,
        skip_keys=skip_keys,
        location=location,
        serialize=serialize,
        deserialize=deserialize,
    )
    schedule = dask.base.get_scheduler(scheduler=scheduler) or dask.get
    return schedule(cached_dsk, keys)
