"""Report completed preview stages without estimating time or pixel throughput."""
from contextlib import contextmanager
from contextvars import ContextVar

_current = ContextVar("preview_progress", default=None)


@contextmanager
def reporting(publish):
    token = _current.set({"publish": publish, "completed": 0})
    try:
        yield
    finally:
        _current.reset(token)


def advance(completed):
    state = _current.get()
    if state is None:
        return
    completed = min(int(completed), 4)
    if completed <= state["completed"]:
        return
    state["completed"] = completed
    state["publish"](completed)
