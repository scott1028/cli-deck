"""In-memory process records and per-process output ring buffers."""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

RING_LIMIT = 200_000  # ~200 KB of recent output per process

STATE_RUNNING = "running"
STATE_STOPPING = "stopping"
STATE_EXITED = "exited"


class RingBuffer:
    """Byte buffer capped at `limit`, dropping the oldest bytes first."""

    def __init__(self, limit: int = RING_LIMIT) -> None:
        self.limit = limit
        self._buf = bytearray()

    def append(self, data: bytes) -> None:
        if not data:
            return
        self._buf.extend(data)
        overflow = len(self._buf) - self.limit
        if overflow > 0:
            del self._buf[:overflow]

    def get(self) -> bytes:
        return bytes(self._buf)

    def __len__(self) -> int:
        return len(self._buf)


@dataclass
class ProcessRecord:
    id: str
    name: str
    argv: list[str]
    cwd: str
    state: str = STATE_RUNNING
    exit_code: int | None = None
    started_at: float = field(default_factory=time.time)
    log_path: str = ""
    ring: RingBuffer = field(default_factory=RingBuffer, repr=False)

    def to_public(self) -> dict:
        """JSON-safe view without the ring buffer."""
        return {
            "id": self.id,
            "name": self.name,
            "argv": self.argv,
            "cwd": self.cwd,
            "state": self.state,
            "exit_code": self.exit_code,
            "started_at": self.started_at,
            "log_path": self.log_path,
        }


class Registry:
    def __init__(self) -> None:
        self._records: dict[str, ProcessRecord] = {}

    def create(self, name: str, argv: list[str], cwd: str, log_path: str = "") -> ProcessRecord:
        record = ProcessRecord(id=uuid.uuid4().hex[:8], name=name,
                               argv=list(argv), cwd=cwd, log_path=log_path)
        self._records[record.id] = record
        return record

    def get(self, proc_id: str) -> ProcessRecord | None:
        return self._records.get(proc_id)

    def remove(self, proc_id: str) -> None:
        self._records.pop(proc_id, None)

    def all(self) -> list[ProcessRecord]:
        return list(self._records.values())
