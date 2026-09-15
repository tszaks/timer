from __future__ import annotations

import fcntl
import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

TIMER_SCHEMA = "timer.v1"
EVENT_SCHEMA = "timer.event.v1"


def iso_time(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp).astimezone().isoformat(timespec="seconds")


def events_path(state_file: Path) -> Path:
    override = os.environ.get("TIMER_EVENTS")
    return Path(override).expanduser() if override else state_file.with_name("events.jsonl")


@contextmanager
def event_lock(state_file: Path, *, exclusive: bool):
    path = events_path(state_file).with_suffix(".lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def make_event(
    source: dict[str, Any], event: str, *, timestamp: float | None = None, extra: dict[str, Any] | None = None
) -> dict[str, Any]:
    timestamp = time.time() if timestamp is None else timestamp
    payload: dict[str, Any] = {
        "ok": True,
        "schema": EVENT_SCHEMA,
        "event_id": str(uuid.uuid4()),
        "event": event,
        "timestamp": iso_time(timestamp),
        "id": source["id"],
        "key": source.get("key") or source.get("label"),
        "label": source.get("label"),
        "namespace": source.get("namespace", "default"),
        "owner": source.get("owner", "local"),
    }
    for field in ("message", "ref"):
        if source.get(field) is not None:
            payload[field] = source[field]
    if source.get("payload") is not None:
        payload["payload"] = source["payload"]
    if "due_at" in source:
        payload["due_at"] = iso_time(source["due_at"])
        payload["remaining_seconds"] = max(0, round(source["due_at"] - timestamp, 3))
    if source.get("status") is not None:
        payload["status"] = source["status"]
    if extra:
        payload.update(extra)
    return payload


def append_event(state_file: Path, payload: dict[str, Any]) -> None:
    path = events_path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    with event_lock(state_file, exclusive=True), path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, separators=(",", ":")) + "\n")
        handle.flush()
        os.fsync(handle.fileno())


def emit_once(
    state_file: Path,
    source: dict[str, Any],
    event: str,
    *,
    timestamp: float | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    emitted = source.setdefault("emitted_events", {})
    if event in emitted:
        return None
    payload = make_event(source, event, timestamp=timestamp, extra=extra)
    append_event(state_file, payload)
    emitted[event] = payload["event_id"]
    return payload


def read_events(state_file: Path) -> list[dict[str, Any]]:
    path = events_path(state_file)
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    with event_lock(state_file, exclusive=False), path.open("r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                events.append(json.loads(line))
    return events


def read_event_records(
    state_file: Path, offset: int = 0
) -> tuple[list[tuple[dict[str, Any], int, int]], int]:
    path = events_path(state_file)
    if not path.exists():
        return [], 0
    records: list[tuple[dict[str, Any], int, int]] = []
    with event_lock(state_file, exclusive=False), path.open("rb") as handle:
        handle.seek(offset)
        while True:
            start = handle.tell()
            line = handle.readline()
            if not line:
                return records, handle.tell()
            end = handle.tell()
            if line.strip():
                records.append((json.loads(line), start, end))


def schema_document() -> dict[str, Any]:
    return {
        "ok": True,
        "schema": TIMER_SCHEMA,
        "contracts": {
            "command": {
                "schema": TIMER_SCHEMA,
                "required": ["ok", "schema", "event"],
                "errors": [
                    "ambiguous", "cancelled", "conflict", "consumer_conflict",
                    "consumer_not_found", "compaction_recovery_required", "hook_failed", "invalid", "lease_mismatch",
                    "not_found", "owner_mismatch",
                ],
            },
            "event": {
                "schema": EVENT_SCHEMA,
                "required": ["ok", "schema", "event_id", "event", "timestamp", "id", "key", "namespace", "owner"],
                "events": ["started", "expired", "cancelled", "lap", "tick"],
            },
        },
    }
