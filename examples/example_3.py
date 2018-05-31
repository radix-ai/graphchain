import logging
from time import sleep

import dask

from graphchain import optimize
from graphchain.errors import GraphchainCompressionMismatch

logging.getLogger("graphchain.graphchain").setLevel(logging.DEBUG)
logging.getLogger("graphchain.funcutils").setLevel(logging.INFO)


def delayed_graph_example():

    # Functions
    @dask.delayed
    def foo(argument):
        sleep(1)
        return argument

    @dask.delayed
    def bar(argument):
        sleep(1)
        return argument + 2

    @dask.delayed
    def baz(*args):
        sleep(1)
        return sum(args)

    @dask.delayed
    def boo(*args):
        sleep(1)
        return len(args) + sum(args)

    @dask.delayed
    def goo(*args):
        sleep(1)
        return sum(args) + 1

    @dask.delayed
    def top(argument, argument2):
        sleep(3)
        return argument - argument2

    # Constants
    v1 = dask.delayed(1)
    v2 = dask.delayed(2)
    v3 = dask.delayed(3)
    v4 = dask.delayed(0)
    v5 = dask.delayed(-1)
    v6 = dask.delayed(-2)
    d1 = dask.delayed(-3)
    boo1 = boo(foo(v1), bar(v2), baz(v3))
    goo1 = goo(foo(v4), v6, bar(v5))
    baz2 = baz(boo1, goo1)
    top1 = top(d1, baz2)
    skipkeys = [boo1.key]
    return (top1, -14, skipkeys)  # DAG and expected result


def compute_with_graphchain(dsk, skipkeys):
    cachedir = "./__hashchain__"
    try:
        with dask.set_options(delayed_optimize=optimize):
            result = dsk.compute(
                compression=True,
                no_cache_keys=skipkeys,
                cachedir=cachedir,
                persistency="local",
                s3bucket="")
            return result
    except GraphchainCompressionMismatch:
        print("[ERROR] Hashchain compression option mismatch.")


def compute_with_graphchain_s3(dsk, skipkeys):
    cachedir = "__hashchain__"
    try:
        with dask.set_options(delayed_optimize=optimize):
            result = dsk.compute(
                compression=True,
                no_cache_keys=skipkeys,
                cachedir=cachedir,
                persistency="s3",
                s3bucket="graphchain-test-bucket")
            return result
    except GraphchainCompressionMismatch:
        print("[ERROR] Hashchain compression option mismatch.")
    except Exception:
        print("[ERROR] Unknown error somewhere.")


def test_example():
    dsk, result, skipkeys = delayed_graph_example()
    try:
        assert compute_with_graphchain(dsk, skipkeys) == result
    except AssertionError:
        print("[ERROR] Results did not match.")


def test_example_s3():
    dsk, result, skipkeys = delayed_graph_example()
    try:
        assert compute_with_graphchain_s3(dsk, skipkeys) == result
    except AssertionError:
        print("[ERROR] Results did not match.")


if __name__ == "__main__":
    test_example()
