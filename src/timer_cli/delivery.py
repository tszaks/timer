from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from typing import Any

from .scheduler import event_lock, events_path, iso_time, read_event_records

CLAIM_SCHEMA = "timer.claim.v1"


class DeliveryError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def consumers_path(state_file: Path) -> Path:
    override = os.environ.get("TIMER_CONSUMERS")
    if override:
        return Path(override).expanduser()
    event_path = events_path(state_file)
    name = "consumers.json" if event_path.name == "events.jsonl" else f"{event_path.name}.consumers.json"
    return event_path.with_name(name)


def compaction_journal_path(state_file: Path) -> Path:
    path = consumers_path(state_file)
    return path.with_name(f"{path.name}.compact-journal")


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)
        fsync_directory(path.parent)
    finally:
        temporary.unlink(missing_ok=True)


def fsync_directory(path: Path) -> None:
    directory_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def recover_compaction(state_file: Path, state: dict[str, Any], registry_path: Path) -> None:
    journal_path = compaction_journal_path(state_file)
    if not journal_path.exists():
        return
    journal = json.loads(journal_path.read_text(encoding="utf-8"))
    event_path = events_path(state_file)
    current = event_path.read_bytes() if event_path.exists() else b""
    old_size = int(journal["old_event_size"])
    new_size = int(journal["new_event_size"])
    has_old_prefix = len(current) >= old_size and digest(current[:old_size]) == journal["old_event_digest"]
    has_new_prefix = len(current) >= new_size and digest(current[:new_size]) == journal["new_event_digest"]
    if has_old_prefix:
        pass
    elif has_new_prefix:
        if int(state.get("generation", 0)) < int(journal["target_generation"]):
            state.clear()
            state.update(journal["consumer_state_after"])
            write_json_atomic(registry_path, state)
    else:
        raise DeliveryError(
            "compaction_recovery_required",
            "event log changed during interrupted compaction; preserve the journal and inspect manually",
        )
    journal_path.unlink(missing_ok=True)
    fsync_directory(journal_path.parent)


@contextmanager
def locked_consumers(state_file: Path) -> Iterator[dict[str, Any]]:
    path = consumers_path(state_file)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        raw = path.read_text(encoding="utf-8") if path.exists() else ""
        state = json.loads(raw) if raw else {"version": 1, "consumers": {}}
        state.setdefault("version", 1)
        state.setdefault("generation", 0)
        state.setdefault("consumers", {})
        recover_compaction(state_file, state, path)
        yield state
        write_json_atomic(path, state)


def event_matches(event: dict[str, Any], filters: dict[str, str | None]) -> bool:
    if event.get("namespace", "default") != filters["namespace"]:
        return False
    if filters.get("owner") and event.get("owner", "local") != filters["owner"]:
        return False
    if filters.get("event_type"):
        accepted = {value.strip() for value in str(filters["event_type"]).split(",")}
        if event.get("event") not in accepted:
            return False
    return True


def consumer_record(
    state: dict[str, Any],
    state_file: Path,
    consumer: str,
    filters: dict[str, str | None],
    acknowledged_event_ids: set[str] | None = None,
) -> dict[str, Any]:
    record = state["consumers"].get(consumer)
    if record is None:
        legacy_skip: list[str] = []
        if acknowledged_event_ids:
            records, _ = read_event_records(state_file)
            present = {event["event_id"] for event, _, _ in records}
            legacy_skip = sorted(acknowledged_event_ids & present)
        record = {
            "cursor": 0,
            "filters": filters,
            "lease": None,
            "legacy_skip_event_ids": legacy_skip,
        }
        state["consumers"][consumer] = record
        return record
    if record.get("filters", filters) != filters:
        raise DeliveryError(
            "consumer_conflict",
            f'consumer "{consumer}" is already bound to different filters',
        )
    record.setdefault("cursor", 0)
    record.setdefault("filters", filters)
    record.setdefault("lease", None)
    record.setdefault("legacy_skip_event_ids", [])
    return record


def seed_consumer_at_tail(
    state_file: Path,
    *,
    consumer: str,
    namespace: str,
    owner: str | None = None,
    event_type: str | None = None,
) -> dict[str, Any]:
    """Register a new delivery path without replaying the retained event log."""
    filters = {"namespace": namespace, "owner": owner, "event_type": event_type}
    with locked_consumers(state_file) as state:
        existing = state["consumers"].get(consumer)
        if existing is not None:
            if existing.get("filters", filters) != filters:
                raise DeliveryError(
                    "consumer_conflict",
                    f'consumer "{consumer}" is already bound to different filters',
                )
            return {"consumer": consumer, "created": False, "cursor": int(existing.get("cursor", 0))}
        _, end = read_event_records(state_file)
        state["consumers"][consumer] = {
            "cursor": end,
            "filters": filters,
            "lease": None,
            "legacy_skip_event_ids": [],
        }
        return {"consumer": consumer, "created": True, "cursor": end}


def list_consumers(state_file: Path) -> list[dict[str, Any]]:
    event_path = events_path(state_file)
    event_size = event_path.stat().st_size if event_path.exists() else 0
    now = time.time()
    with locked_consumers(state_file) as state:
        result = []
        for name, record in sorted(state["consumers"].items()):
            cursor = int(record.get("cursor", 0))
            lease = record.get("lease")
            item: dict[str, Any] = {
                "name": name,
                "cursor": cursor,
                "lag_bytes": max(0, event_size - cursor),
                "filters": record.get("filters", {}),
                "lease": "active" if lease and lease.get("lease_until", 0) > now else "none",
            }
            if lease:
                item["leased_event_id"] = lease.get("event_id")
                item["lease_until"] = iso_time(lease.get("lease_until", 0))
            result.append(item)
        return result


def forget_consumer(state_file: Path, *, consumer: str, force: bool = False) -> dict[str, Any]:
    with locked_consumers(state_file) as state:
        record = state["consumers"].get(consumer)
        if record is None:
            raise DeliveryError("consumer_not_found", f'consumer not found: {consumer}')
        lease = record.get("lease")
        if lease and lease.get("lease_until", 0) > time.time() and not force:
            raise DeliveryError(
                "consumer_busy",
                f'consumer "{consumer}" has an active lease; retry after it expires or use --force',
            )
        del state["consumers"][consumer]
    return {"consumer": consumer, "event": "forgotten"}


def claim_event(
    state_file: Path,
    *,
    consumer: str,
    lease_seconds: float,
    namespace: str,
    owner: str | None = None,
    event_type: str | None = None,
    acknowledged_event_ids: set[str] | None = None,
) -> dict[str, Any]:
    now = time.time()
    filters = {"namespace": namespace, "owner": owner, "event_type": event_type}
    with locked_consumers(state_file) as state:
        record = consumer_record(
            state, state_file, consumer, filters, acknowledged_event_ids
        )
        lease = record.get("lease")
        if lease and lease["lease_until"] > now:
            return {
                "ok": True,
                "schema": CLAIM_SCHEMA,
                "event": "busy",
                "consumer": consumer,
                "leased_event_id": lease["event_id"],
                "lease_until": iso_time(lease["lease_until"]),
            }
        record["lease"] = None
        records, end = read_event_records(state_file, int(record["cursor"]))
        for event, start, next_offset in records:
            legacy_skip = set(record.get("legacy_skip_event_ids", []))
            if event["event_id"] in legacy_skip:
                legacy_skip.remove(event["event_id"])
                record["legacy_skip_event_ids"] = sorted(legacy_skip)
                record["cursor"] = next_offset
                continue
            if not event_matches(event, filters):
                record["cursor"] = next_offset
                continue
            lease_until = now + lease_seconds
            record["cursor"] = start
            lease_id = str(uuid.uuid4())
            record["lease"] = {
                "lease_id": lease_id,
                "event_id": event["event_id"],
                "offset": start,
                "next_offset": next_offset,
                "lease_until": lease_until,
            }
            return {
                "ok": True,
                "schema": CLAIM_SCHEMA,
                "event": "claimed",
                "consumer": consumer,
                "lease_id": lease_id,
                "lease_until": iso_time(lease_until),
                "delivery": event,
            }
        record["cursor"] = end
        return {
            "ok": True,
            "schema": CLAIM_SCHEMA,
            "event": "empty",
            "consumer": consumer,
        }


def ack_event(
    state_file: Path, *, consumer: str, event_id: str, lease_id: str
) -> dict[str, Any]:
    with locked_consumers(state_file) as state:
        record = state["consumers"].get(consumer)
        if record is None:
            raise DeliveryError("consumer_not_found", f'consumer not found: {consumer}')
        if (
            record.get("last_acked_event_id") == event_id
            and record.get("last_acked_lease_id") == lease_id
        ):
            return {
                "ok": True,
                "schema": CLAIM_SCHEMA,
                "event": "already_acked",
                "consumer": consumer,
                "event_id": event_id,
                "lease_id": lease_id,
            }
        lease = record.get("lease")
        if (
            not lease
            or lease["event_id"] != event_id
            or lease.get("lease_id") != lease_id
        ):
            raise DeliveryError("lease_mismatch", f'event is not leased by consumer "{consumer}"')
        record["cursor"] = lease["next_offset"]
        record["last_acked_event_id"] = event_id
        record["last_acked_lease_id"] = lease_id
        record["lease"] = None
    return {
        "ok": True,
        "schema": CLAIM_SCHEMA,
        "event": "acked",
        "consumer": consumer,
        "event_id": event_id,
        "lease_id": lease_id,
    }


def nack_event(
    state_file: Path, *, consumer: str, event_id: str, lease_id: str
) -> dict[str, Any]:
    with locked_consumers(state_file) as state:
        record = state["consumers"].get(consumer)
        if record is None:
            raise DeliveryError("consumer_not_found", f'consumer not found: {consumer}')
        lease = record.get("lease")
        if (
            not lease
            or lease["event_id"] != event_id
            or lease.get("lease_id") != lease_id
        ):
            raise DeliveryError("lease_mismatch", f'event is not leased by consumer "{consumer}"')
        record["lease"] = None
    return {
        "ok": True,
        "schema": CLAIM_SCHEMA,
        "event": "nacked",
        "consumer": consumer,
        "event_id": event_id,
        "lease_id": lease_id,
    }


def peek_events(
    state_file: Path,
    *,
    consumer: str,
    namespace: str,
    owner: str | None = None,
    event_type: str | None = None,
    acknowledged_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    filters = {"namespace": namespace, "owner": owner, "event_type": event_type}
    with locked_consumers(state_file) as state:
        record = consumer_record(
            state, state_file, consumer, filters, acknowledged_event_ids
        )
        lease = record.get("lease")
        if lease and lease["lease_until"] > time.time():
            return []
        records, _ = read_event_records(state_file, int(record["cursor"]))
        legacy_skip = set(record.get("legacy_skip_event_ids", []))
        return [
            event
            for event, _, _ in records
            if event["event_id"] not in legacy_skip and event_matches(event, filters)
        ]


def drain_events(
    state_file: Path,
    *,
    consumer: str,
    namespace: str,
    owner: str | None = None,
    event_type: str | None = None,
    acknowledged_event_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    drained: list[dict[str, Any]] = []
    while True:
        claimed = claim_event(
            state_file,
            consumer=consumer,
            lease_seconds=60,
            namespace=namespace,
            owner=owner,
            event_type=event_type,
            acknowledged_event_ids=acknowledged_event_ids,
        )
        if claimed["event"] != "claimed":
            return drained
        event = claimed["delivery"]
        drained.append(event)
        ack_event(
            state_file,
            consumer=consumer,
            event_id=event["event_id"],
            lease_id=claimed["lease_id"],
        )


def compact_events(state_file: Path) -> dict[str, Any]:
    path = events_path(state_file)
    if not path.exists():
        return {"bytes_removed": 0, "events_removed": 0, "consumers": 0, "removed_event_ids": []}
    with locked_consumers(state_file) as state:
        named_consumers = list(state["consumers"].items())
        consumers = [record for _, record in named_consumers]
        if not consumers:
            return {"bytes_removed": 0, "events_removed": 0, "consumers": 0, "removed_event_ids": []}
        cutoff = min(int(record.get("cursor", 0)) for record in consumers)
        blockers = sorted(
            name for name, record in named_consumers if int(record.get("cursor", 0)) == cutoff
        )
        if cutoff <= 0:
            if path.stat().st_size:
                names = ", ".join(blockers)
                raise DeliveryError(
                    "compaction_blocked",
                    f"compaction blocked by consumer(s): {names}; inspect with `timer consumers` or remove with `timer forget NAME`",
                )
            return {
                "bytes_removed": 0,
                "events_removed": 0,
                "consumers": len(consumers),
                "removed_event_ids": [],
                "blockers": blockers,
            }
        registry_path = consumers_path(state_file)
        journal_path = compaction_journal_path(state_file)
        with event_lock(state_file, exclusive=True):
            with path.open("rb") as handle:
                prefix = handle.read(cutoff)
                remainder = handle.read()
            removed = [json.loads(line) for line in prefix.splitlines() if line.strip()]
            state_after = deepcopy(state)
            state_after["generation"] = int(state.get("generation", 0)) + 1
            for record in state_after["consumers"].values():
                record["cursor"] = max(0, int(record.get("cursor", 0)) - cutoff)
                lease = record.get("lease")
                if lease:
                    lease["offset"] = max(0, int(lease["offset"]) - cutoff)
                    lease["next_offset"] = max(0, int(lease["next_offset"]) - cutoff)
            old_data = prefix + remainder
            journal = {
                "schema": "timer.compaction-journal.v1",
                "old_event_digest": digest(old_data),
                "old_event_size": len(old_data),
                "new_event_digest": digest(remainder),
                "new_event_size": len(remainder),
                "target_generation": state_after["generation"],
                "consumer_state_after": state_after,
            }
            write_json_atomic(journal_path, journal)
            temporary = path.with_name(f".{path.name}.compact-{os.getpid()}")
            with temporary.open("wb") as handle:
                handle.write(remainder)
                handle.flush()
                os.fsync(handle.fileno())
            temporary.replace(path)
            fsync_directory(path.parent)
            state.clear()
            state.update(state_after)
            write_json_atomic(registry_path, state)
            journal_path.unlink(missing_ok=True)
            fsync_directory(journal_path.parent)
        return {
            "bytes_removed": cutoff,
            "events_removed": len(removed),
            "consumers": len(consumers),
            "removed_event_ids": [event["event_id"] for event in removed],
            "blockers": blockers,
        }
