"""Bounded synchronous model work, with draining before propagating failures."""
from __future__ import annotations

import threading
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait


class BuildStopped(Exception):
    pass


def parallel_map(function, items, workers: int, *, check=lambda: None, on_error=lambda: None):
    """Keep at most ``workers`` tasks submitted and return results in input order.

    An error stops dispatch immediately. Running tasks are joined so their valid
    model responses can be checkpointed. A real failure wins over cancellation
    raised by a sibling observing the shared abort flag.
    """
    halted, mutex = threading.Event(), threading.Lock()
    errors, results = [], {}

    def record(error):
        with mutex:
            errors.append(error)
            halted.set()
        on_error()

    def guarded(item):
        try:
            if halted.is_set():
                raise BuildStopped()
            check()
            return function(item)
        except Exception as exc:
            record(exc)
            raise

    iterator = iter(enumerate(items))
    pending = {}
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="graph-unit") as pool:
        def fill():
            while len(pending) < workers and not halted.is_set():
                try:
                    check()
                    index, item = next(iterator)
                except StopIteration:
                    break
                except Exception as exc:
                    record(exc)
                    break
                pending[pool.submit(guarded, item)] = index

        fill()
        while pending:
            done, _ = wait(pending, timeout=0.05, return_when=FIRST_COMPLETED)
            for future in done:
                index = pending.pop(future)
                try:
                    results[index] = future.result()
                except Exception:
                    # guarded() already recorded the original exception.
                    pass
            if not halted.is_set():
                fill()
    if errors:
        raise next((e for e in errors if not isinstance(e, BuildStopped)), errors[0])
    check()
    return [results[index] for index in sorted(results)]
