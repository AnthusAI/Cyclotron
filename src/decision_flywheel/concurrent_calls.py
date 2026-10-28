"""Run independent blocking calls at once, one thread each, and return their results in the order given.

Used for per-output storage work: each output's runtime journal is its own object over its own keys, so its
acquire, commit and release can go out together instead of one after another. Every call runs to its end
even when another fails, so nothing is left half-started, and then the first failure in the given order is
raised. Each call runs in its own copy of the caller's context.
"""

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context


def call_concurrently(calls):
    """Call each of ``calls`` (no arguments) at once; return their results in order, or raise the first error."""
    calls = list(calls)
    if len(calls) <= 1:
        return [call() for call in calls]
    with ThreadPoolExecutor(len(calls)) as pool:
        futures = [pool.submit(copy_context().run, call) for call in calls]
        outcomes = []
        for future in futures:
            try:
                outcomes.append((True, future.result()))
            except BaseException as error:  # noqa: BLE001 - every call settles before the first failure is raised
                outcomes.append((False, error))
    for succeeded, value in outcomes:
        if not succeeded:
            raise value
    return [value for _, value in outcomes]


def call_serially(calls):
    """The same contract, one call after another: for stores that must stay on one thread (SQLite)."""
    results, failure = [], None
    for call in calls:
        try:
            results.append(call())
        except BaseException as error:  # noqa: BLE001
            failure = failure or error
    if failure is not None:
        raise failure
    return results
