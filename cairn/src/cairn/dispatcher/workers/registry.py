from __future__ import annotations

from cairn.dispatcher.workers.adapters import MockDriver
from cairn.dispatcher.workers.base import WorkerDriver


_MOCK = MockDriver()

DRIVERS: dict[str, WorkerDriver] = {
    "mock": _MOCK,
}


def get_driver(name: str) -> WorkerDriver:
    """Return a process-based driver. Native workers do not use a driver."""
    return DRIVERS[name]
