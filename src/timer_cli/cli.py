from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .scheduler import (
    TIMER_SCHEMA,
    append_event,
    emit_once,
    iso_time,
    make_event,
    read_events,
    schema_document,
)


DURATION_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)(h|m|s)")
TOP_LEVEL_COMMANDS = {
    "start", "list", "status", "cancel", "rename", "wait", "watch", "pending", "drain",
    "daemon", "every", "schema", "stopwatch", "-h", "--help",
}
STOPWATCH_COMMANDS = {"start", "list", "status", "pause", "resume", "lap", "reset", "stop", "-h", "--help"}
STOPWATCH_SHORTCUTS = {"lap", "pause", "resume", "reset"}


class TimerError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def namespace_for(args: argparse.Namespace) -> str:
    return getattr(args, "namespace", None) or os.environ.get("TIMER_NAMESPACE", "default")


def owner_for(args: argparse.Namespace) -> str:
    return getattr(args, "owner", None) or os.environ.get("TIMER_OWNER", "local")


def parse_duration(value: str) -> float:
    value = value.strip()
    if value.isdigit():
        seconds = float(value)
    else:
        position = 0
        seconds = 0.0
        multipliers = {"h": 3600, "m": 60, "s": 1}
        for match in DURATION_RE.finditer(value):
            if match.start() != position:
                raise ValueError(f"invalid duration: {value}")
            seconds += float(match.group(1)) * multipliers[match.group(2).lower()]
            position = match.end()
        if position != len(value) or position == 0:
            raise ValueError(f"invalid duration: {value}")
    if seconds <= 0:
        raise ValueError("duration must be greater than zero")
    return seconds


def state_path() -> Path:
    override = os.environ.get("TIMER_STATE")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "timer" / "timers.json"


@contextmanager
def locked_state() -> Iterator[tuple[dict[str, Any], Any]]:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        raw = handle.read()
        state = json.loads(raw) if raw else {}
        state.setdefault("timers", [])
        state.setdefault("stopwatches", [])
        state.setdefault("series", [])
        state.setdefault("acked_event_ids", [])
        yield state, handle
        handle.seek(0)
        handle.truncate()
        json.dump(state, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())


def read_state() -> dict[str, Any]:
    """Read state under a shared lock without creating or changing the file."""
    path = state_path()
    if not path.exists():
        return {"timers": [], "stopwatches": [], "series": [], "acked_event_ids": []}
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        raw = handle.read()
    state = json.loads(raw) if raw else {}
    state.setdefault("timers", [])
    state.setdefault("stopwatches", [])
    state.setdefault("series", [])
    state.setdefault("acked_event_ids", [])
    return state


def refresh(
    timer: dict[str, Any], now: float | None = None, *, materialize_event: bool = False
) -> dict[str, Any]:
    now = time.time() if now is None else now
    if timer["status"] == "active" and now >= timer["due_at"]:
        timer["status"] = "expired"
        timer["expired_at"] = timer["due_at"]
        if materialize_event:
            emit_once(state_path(), timer, "expired", timestamp=timer["due_at"])
    timer["remaining_seconds"] = max(0, round(timer["due_at"] - now, 3))
    return timer


def find_item(items: list[dict[str, Any]], identifier: str, kind: str) -> dict[str, Any]:
    exact = [item for item in items if item["id"] == identifier]
    if exact:
        return exact[0]
    label_matches = [item for item in items if item["label"].casefold() == identifier.casefold()]
    if len(label_matches) == 1:
        return label_matches[0]
    if len(label_matches) > 1:
        raise ValueError(
            f'multiple {kind}s are named "{identifier}"; choose one: {choice_labels(label_matches)}'
        )
    matches = [item for item in items if item["id"].startswith(identifier)]
    if not matches:
        raise KeyError(f"{kind} not found: {identifier}")
    if len(matches) > 1:
        raise ValueError(f"{kind} ID prefix is ambiguous: {identifier}")
    return matches[0]


def find_timer(
    state: dict[str, Any], identifier: str, *, namespace: str = "default", by_key: bool = False
) -> dict[str, Any]:
    timers = [timer for timer in state["timers"] if timer.get("namespace", "default") == namespace]
    if by_key:
        matches = [timer for timer in timers if (timer.get("key") or timer["label"]).casefold() == identifier.casefold()]
        active = [timer for timer in matches if timer["status"] == "active"]
        if len(active) == 1:
            return active[0]
        if len(active) > 1:
            raise TimerError("ambiguous", f'timer key is ambiguous: {identifier}')
        if matches:
            return max(matches, key=lambda timer: timer["created_at"])
        raise TimerError("not_found", f"timer not found: {identifier}")
    exact = [timer for timer in timers if timer["id"] == identifier]
    if exact:
        return exact[0]
    label_matches = [
        timer
        for timer in timers
        if timer["label"].casefold() == identifier.casefold()
    ]
    active_matches = [timer for timer in label_matches if timer["status"] == "active"]
    if len(active_matches) == 1:
        return active_matches[0]
    if len(active_matches) > 1:
        raise ValueError(
            f'multiple active timers are named "{identifier}"; choose one: '
            f'{choice_labels(active_matches)}'
        )
    if label_matches:
        return max(label_matches, key=lambda timer: timer["created_at"])
    return find_item(timers, identifier, "timer")


def find_stopwatch(state: dict[str, Any], identifier: str) -> dict[str, Any]:
    return find_item(state["stopwatches"], identifier, "stopwatch")


def public_timer(
    timer: dict[str, Any], now: float | None = None, *, event: str = "status"
) -> dict[str, Any]:
    timer = refresh(dict(timer), now)
    original_seconds = timer["due_at"] - timer["created_at"]
    display_unit = timer.get("display_unit") or inferred_unit(original_seconds)
    result = {
        "ok": True,
        "schema": TIMER_SCHEMA,
        "event": event,
        "id": timer["id"],
        "key": timer.get("key") or timer["label"],
        "label": timer["label"],
        "status": timer["status"],
        "remaining_seconds": timer["remaining_seconds"],
        "display_unit": display_unit,
        "due_at": datetime.fromtimestamp(timer["due_at"]).astimezone().isoformat(timespec="seconds"),
        "namespace": timer.get("namespace", "default"),
        "owner": timer.get("owner", "local"),
    }
    for field in ("message", "ref"):
        if timer.get(field) is not None:
            result[field] = timer[field]
    return result


def inferred_unit(seconds: float) -> str:
    if seconds >= 3600:
        return "h"
    if seconds >= 60:
        return "m"
    return "s"


def duration_unit(value: str) -> str:
    units = {match.group(2).lower() for match in DURATION_RE.finditer(value)}
    if "h" in units:
        return "h"
    if "m" in units:
        return "m"
    return "s"


def adaptive_time(seconds: float, largest_unit: str | None = None, *, round_up: bool = False) -> str:
    total = math.ceil(seconds) if round_up else math.floor(seconds)
    total = max(0, total)
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    largest_unit = largest_unit or inferred_unit(total)
    if largest_unit == "h":
        return f"{hours}h {minutes:02d}m {seconds:02d}s"
    if largest_unit == "m":
        return f"{hours * 60 + minutes}m {seconds:02d}s"
    return f"{total}s"


def emit(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, separators=(",", ":")))
        return
    if isinstance(payload, list):
        if not payload:
            print("No timers.")
        for timer in payload:
            print(human_timer(timer))
        return
    print(human_timer(payload))


def human_timer(timer: dict[str, Any]) -> str:
    status = timer["status"]
    if status == "expired":
        return f'{timer["label"]}: expired'
    remaining = adaptive_time(
        timer["remaining_seconds"], timer["display_unit"], round_up=status == "active"
    )
    if status == "cancelled":
        return f'{timer["label"]}: cancelled ({remaining} remaining)'
    return f'{timer["label"]}: {remaining} remaining'


def choice_labels(items: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for item in items:
        key = item["label"].casefold()
        counts[key] = counts.get(key, 0) + 1
    choices = []
    for item in items:
        label = item["label"]
        if counts[label.casefold()] > 1:
            peer_ids = [peer["id"] for peer in items if peer["label"].casefold() == label.casefold()]
            length = 4
            while length < len(item["id"]) and len({peer[:length] for peer in peer_ids}) < len(peer_ids):
                length += 1
            label = f'{label} [{item["id"][:length]}]'
        choices.append(label)
    return ", ".join(choices)


def suggested_label(label: str, active_items: list[dict[str, Any]]) -> str:
    used = {item["label"].casefold() for item in active_items}
    suffix = 2
    while f"{label}-{suffix}".casefold() in used:
        suffix += 1
    return f"{label}-{suffix}"


def ensure_unique_active_label(
    label: str, active_items: list[dict[str, Any]], *, exclude_id: str | None = None
) -> None:
    conflicts = [
        item
        for item in active_items
        if item["id"] != exclude_id and item["label"].casefold() == label.casefold()
    ]
    if conflicts:
        suggestion = suggested_label(label, active_items)
        raise ValueError(f'label "{label}" is already active; try "{suggestion}"')


def resolve_timer_arg(state: dict[str, Any], args: argparse.Namespace) -> dict[str, Any]:
    namespace = namespace_for(args)
    if getattr(args, "id", None):
        return find_timer(state, args.id, namespace=namespace)
    if getattr(args, "key", None):
        return find_timer(state, args.key, namespace=namespace, by_key=True)
    if getattr(args, "identifier", None):
        return find_timer(state, args.identifier, namespace=namespace)
    raise TimerError("invalid", "a timer identifier, --id, or --key is required")


def require_owner(timer: dict[str, Any], args: argparse.Namespace) -> None:
    if getattr(args, "force", False):
        return
    expected = owner_for(args)
    actual = timer.get("owner", "local")
    if actual != expected:
        raise TimerError(
            "owner_mismatch",
            f'timer is owned by "{actual}"; use the matching --owner or --force',
        )


def command_start(args: argparse.Namespace) -> int:
    duration = parse_duration(args.duration)
    now = time.time()
    namespace = namespace_for(args)
    owner = owner_for(args)
    label = args.label or args.key
    if not label:
        raise TimerError("invalid", "--label or --key is required")
    key = args.key
    timer = {
        "id": str(uuid.uuid4()),
        "key": key,
        "label": label,
        "status": "active",
        "created_at": now,
        "due_at": now + duration,
        "duration_seconds": duration,
        "display_unit": duration_unit(args.duration),
        "message": args.message,
        "ref": args.ref,
        "namespace": namespace,
        "owner": owner,
    }
    with locked_state() as (state, _):
        for item in state["timers"]:
            refresh(item, now, materialize_event=True)
        active = [
            item
            for item in state["timers"]
            if item["status"] == "active" and item.get("namespace", "default") == namespace
        ]
        keyed = [
            item for item in active
            if key is not None and item.get("key") is not None and item["key"].casefold() == key.casefold()
        ]
        if keyed:
            existing = keyed[0]
            if existing.get("owner", "local") != owner:
                raise TimerError("owner_mismatch", f'timer key "{key}" is owned by another owner')
            existing_duration = existing.get("duration_seconds", existing["due_at"] - existing["created_at"])
            if not math.isclose(existing_duration, duration, abs_tol=0.001):
                raise TimerError(
                    "conflict",
                    f'timer key "{key}" already exists with a different duration',
                )
            emit(public_timer(existing, now, event="existing"), args.json)
            return 0
        if key is not None and any(
            item["status"] == "active"
            and item.get("namespace", "default") == namespace
            and item["key"].casefold() == key.casefold()
            for item in state["series"]
        ):
            raise TimerError("conflict", f'key "{key}" is already used by a recurring schedule')
        ensure_unique_active_label(timer["label"], active)
        state["timers"].append(timer)
        emit_once(state_path(), timer, "started", timestamp=now)
    emit(public_timer(timer, now, event="started"), args.json)
    return 0


def find_series(state: dict[str, Any], identifier: str, namespace: str) -> dict[str, Any]:
    series = [item for item in state.get("series", []) if item.get("namespace", "default") == namespace]
    exact = [item for item in series if item["id"] == identifier]
    if exact:
        return exact[0]
    keyed = [item for item in series if item["key"].casefold() == identifier.casefold()]
    active = [item for item in keyed if item["status"] == "active"]
    if len(active) == 1:
        return active[0]
    if len(active) > 1:
        raise TimerError("ambiguous", f'recurring timer key is ambiguous: {identifier}')
    if keyed:
        return max(keyed, key=lambda item: item["created_at"])
    raise TimerError("not_found", f"timer or recurring schedule not found: {identifier}")


def public_series(series: dict[str, Any], *, event: str = "status") -> dict[str, Any]:
    result: dict[str, Any] = {
        "ok": True,
        "schema": TIMER_SCHEMA,
        "event": event,
        "id": series["id"],
        "key": series["key"],
        "label": series["label"],
        "status": series["status"],
        "interval_seconds": series["interval_seconds"],
        "until_at": iso_time(series["until_at"]),
        "next_at": iso_time(series["next_at"]),
        "tick_count": series["tick_count"],
        "namespace": series.get("namespace", "default"),
        "owner": series.get("owner", "local"),
    }
    for field in ("message", "ref"):
        if series.get(field) is not None:
            result[field] = series[field]
    return result


def command_every(args: argparse.Namespace) -> int:
    interval = parse_duration(args.interval)
    until = parse_duration(args.until)
    if interval > until:
        raise TimerError("invalid", "recurring interval cannot be longer than --until")
    now = time.time()
    namespace = namespace_for(args)
    owner = owner_for(args)
    with locked_state() as (state, _):
        if any(
            item["status"] == "active"
            and item.get("namespace", "default") == namespace
            and item.get("key") is not None
            and item["key"].casefold() == args.key.casefold()
            for item in state["timers"]
        ):
            raise TimerError("conflict", f'key "{args.key}" is already used by a timer')
        existing = [
            item for item in state["series"]
            if item["status"] == "active"
            and item.get("namespace", "default") == namespace
            and item["key"].casefold() == args.key.casefold()
        ]
        if existing:
            series = existing[0]
            if series.get("owner", "local") != owner:
                raise TimerError("owner_mismatch", f'recurring key "{args.key}" is owned by another owner')
            same_contract = math.isclose(series["interval_seconds"], interval, abs_tol=0.001) and math.isclose(
                series["window_seconds"], until, abs_tol=0.001
            )
            if not same_contract:
                raise TimerError("conflict", f'recurring key "{args.key}" already has a different schedule')
            print(json.dumps(public_series(series, event="existing"), separators=(",", ":"))) if args.json else print(
                f'{series["key"]}: recurring schedule already active'
            )
            return 0
        series = {
            "id": str(uuid.uuid4()),
            "key": args.key,
            "label": args.label or args.key,
            "status": "active",
            "created_at": now,
            "interval_seconds": interval,
            "window_seconds": until,
            "next_at": now + interval,
            "until_at": now + until,
            "tick_count": 0,
            "message": args.message,
            "ref": args.ref,
            "namespace": namespace,
            "owner": owner,
        }
        state["series"].append(series)
        emit_once(state_path(), series, "started", timestamp=now, extra={"kind": "series"})
    payload = public_series(series, event="started")
    print(json.dumps(payload, separators=(",", ":"))) if args.json else print(
        f'{series["key"]}: every {adaptive_time(interval)} until {adaptive_time(until)}'
    )
    return 0


def command_list(args: argparse.Namespace) -> int:
    now = time.time()
    namespace = namespace_for(args)
    with locked_state() as (state, _):
        for timer in state["timers"]:
            refresh(timer, now, materialize_event=True)
        timers = [
            public_timer(timer, now)
            for timer in state["timers"]
            if timer.get("namespace", "default") == namespace
        ]
    if not args.all:
        timers = [timer for timer in timers if timer["status"] == "active"]
    if args.mine:
        timers = [timer for timer in timers if timer["owner"] == owner_for(args)]
    if not timers and args.hint_if_empty and not args.json:
        print("No active timers. Start one: timer 10m rice")
        return 0
    emit(timers, args.json)
    return 0


def command_status(args: argparse.Namespace) -> int:
    with locked_state() as (state, _):
        if args.identifier or args.id or args.key:
            timer = resolve_timer_arg(state, args)
            refresh(timer, materialize_event=True)
            result: Any = public_timer(timer)
        else:
            now = time.time()
            for timer in state["timers"]:
                refresh(timer, now, materialize_event=True)
            active = [
                public_timer(timer, now)
                for timer in state["timers"]
                if timer["status"] == "active"
                and timer.get("namespace", "default") == namespace_for(args)
            ]
            result = active[0] if len(active) == 1 else active
    if not args.identifier and not result and not args.json:
        print("No active timers. Start one: timer 10m rice")
        return 0
    emit(result, args.json)
    return 0


def command_cancel(args: argparse.Namespace) -> int:
    with locked_state() as (state, _):
        now = time.time()
        for item in state["timers"]:
            refresh(item, now, materialize_event=True)
        is_series = False
        if args.identifier or args.id or args.key:
            try:
                timer = resolve_timer_arg(state, args)
            except (KeyError, TimerError) as error:
                if isinstance(error, TimerError) and error.code != "not_found":
                    raise
                identifier = args.id or args.key or args.identifier
                timer = find_series(state, identifier, namespace_for(args))
                is_series = True
        else:
            active = [
                item for item in state["timers"]
                if item["status"] == "active"
                and item.get("namespace", "default") == namespace_for(args)
            ]
            if not active:
                raise ValueError("no active timer to cancel")
            if len(active) > 1:
                raise ValueError(f"multiple timers are active; choose one: {choice_labels(active)}")
            timer = active[0]
        require_owner(timer, args)
        event_type = timer["status"]
        if timer["status"] == "active":
            timer["status"] = "cancelled"
            timer["cancelled_at"] = time.time()
            emit_once(state_path(), timer, "cancelled", timestamp=timer["cancelled_at"])
            event_type = "cancelled"
        result = public_series(timer, event=event_type) if is_series else public_timer(timer, event=event_type)
    if is_series:
        print(json.dumps(result, separators=(",", ":"))) if args.json else print(
            f'{timer["key"]}: recurring schedule {timer["status"]}'
        )
    else:
        emit(result, args.json)
    return 0


def command_rename(args: argparse.Namespace) -> int:
    now = time.time()
    new_label = args.new_label_option or args.new_label
    if not new_label:
        raise TimerError("invalid", "a new label is required")
    with locked_state() as (state, _):
        for item in state["timers"]:
            refresh(item, now, materialize_event=True)
        timer = resolve_timer_arg(state, args)
        require_owner(timer, args)
        if timer["status"] != "active":
            raise ValueError("only an active timer can be renamed")
        active = [
            item for item in state["timers"]
            if item["status"] == "active"
            and item.get("namespace", "default") == namespace_for(args)
        ]
        ensure_unique_active_label(new_label, active, exclude_id=timer["id"])
        timer["label"] = new_label
        result = public_timer(timer, now)
    emit(result, args.json)
    return 0


def command_wait(args: argparse.Namespace) -> int:
    if args.any_keys or args.all:
        return command_wait_many(args)
    identifier = args.identifier or args.id or args.key
    by_key = bool(args.key)
    if not identifier:
        raise TimerError("invalid", "a timer identifier, --id, or --key is required")
    while True:
        with locked_state() as (state, _):
            timer = find_timer(state, identifier, namespace=namespace_for(args), by_key=by_key)
            identifier = timer["id"]
            by_key = False
            refresh(timer, materialize_event=True)
            result = public_timer(timer, event=timer["status"] if timer["status"] != "active" else "waiting")
        if result["status"] != "active":
            if args.json:
                print(json.dumps(stored_terminal_event(timer), separators=(",", ":")))
            else:
                emit(result, False)
            return 0 if args.json or result["status"] == "expired" else 2
        time.sleep(min(args.poll_interval, max(0.05, result["remaining_seconds"])))


def command_wait_many(args: argparse.Namespace) -> int:
    namespace = namespace_for(args)
    with locked_state() as (state, _):
        refresh_all(state)
        if args.any_keys:
            keys = [key.strip() for key in args.any_keys.split(",") if key.strip()]
            if not keys:
                raise TimerError("invalid", "--any requires one or more comma-separated keys")
            selected = [find_timer(state, key, namespace=namespace, by_key=True) for key in keys]
        else:
            if not args.key_prefix:
                raise TimerError("invalid", "--all requires --key-prefix")
            matching = [
                timer for timer in state["timers"]
                if timer.get("namespace", "default") == namespace
                and timer.get("key", "").startswith(args.key_prefix)
            ]
            latest_by_key: dict[str, dict[str, Any]] = {}
            for timer in matching:
                key = timer["key"]
                if key not in latest_by_key or timer["created_at"] > latest_by_key[key]["created_at"]:
                    latest_by_key[key] = timer
            selected = list(latest_by_key.values())
            if not selected:
                raise TimerError("not_found", f'no timer keys start with "{args.key_prefix}"')
        ids = [timer["id"] for timer in selected]

    while True:
        with locked_state() as (state, _):
            refresh_all(state)
            timers = [find_timer(state, timer_id, namespace=namespace) for timer_id in ids]
            results = [
                public_timer(timer, event=timer["status"] if timer["status"] != "active" else "waiting")
                for timer in timers
            ]
        complete = [result for result in results if result["status"] != "active"]
        if args.any_keys and complete:
            if args.json:
                completed_timer = next(timer for timer in timers if timer["id"] == complete[0]["id"])
                print(json.dumps(stored_terminal_event(completed_timer), separators=(",", ":")))
            else:
                emit(complete[0], False)
            return 0 if args.json or complete[0]["status"] == "expired" else 2
        if args.all and len(complete) == len(results):
            if args.json:
                print(json.dumps({
                    "ok": True,
                    "schema": TIMER_SCHEMA,
                    "event": "all_complete",
                    "timers": results,
                }, separators=(",", ":")))
            else:
                emit(results, False)
            return 0 if args.json or all(item["status"] == "expired" for item in results) else 2
        remaining = [result["remaining_seconds"] for result in results if result["status"] == "active"]
        time.sleep(min(args.poll_interval, max(0.05, min(remaining))))


def stored_terminal_event(timer: dict[str, Any]) -> dict[str, Any]:
    event_type = timer["status"]
    event_id = timer.get("emitted_events", {}).get(event_type)
    if event_id:
        for event in read_events(state_path()):
            if event["event_id"] == event_id:
                return event
    raise TimerError("invalid", f"terminal event was not materialized for timer {timer['id']}")


def command_watch(args: argparse.Namespace) -> int:
    if args.json:
        return command_follow(args)
    interactive = sys.stdout.isatty()
    try:
        while True:
            now = time.time()
            state = read_state()
            timers = [
                public_timer(timer, now)
                for timer in state["timers"]
                if timer.get("namespace", "default") == namespace_for(args)
            ]
            active = [timer for timer in timers if timer["status"] == "active"]
            if interactive:
                sys.stdout.write("\033[H\033[J")
            print("Active timers")
            if active:
                for timer in active:
                    print(human_timer(timer))
            else:
                print("No active timers.")
            sys.stdout.flush()
            if args.once or not active:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        if interactive:
            print()
        return 0


def refresh_all(state: dict[str, Any], now: float | None = None) -> None:
    now = time.time() if now is None else now
    for timer in state["timers"]:
        refresh(timer, now, materialize_event=True)
    for series in state.get("series", []):
        if series["status"] != "active":
            continue
        emitted = 0
        while series["next_at"] <= now and series["next_at"] <= series["until_at"]:
            series["tick_count"] += 1
            event = make_event(
                series,
                "tick",
                timestamp=series["next_at"],
                extra={
                    "kind": "series",
                    "tick": series["tick_count"],
                    "scheduled_at": iso_time(series["next_at"]),
                },
            )
            append_event(state_path(), event)
            series["next_at"] += series["interval_seconds"]
            emitted += 1
            if emitted >= 100:
                break
        if series["next_at"] > series["until_at"]:
            series["status"] = "completed"


def visible_events(
    state: dict[str, Any], args: argparse.Namespace, *, include_acked: bool = False
) -> list[dict[str, Any]]:
    acknowledged = set(state.get("acked_event_ids", []))
    namespace = namespace_for(args)
    events = [event for event in read_events(state_path()) if event.get("namespace", "default") == namespace]
    if getattr(args, "mine", False):
        events = [event for event in events if event.get("owner", "local") == owner_for(args)]
    event_type = getattr(args, "event_type", None)
    if event_type:
        events = [event for event in events if event.get("event") == event_type]
    if not include_acked:
        events = [event for event in events if event["event_id"] not in acknowledged]
    return events


def emit_events(events: list[dict[str, Any]], as_json: bool) -> None:
    if as_json:
        print(json.dumps(events, separators=(",", ":")))
        return
    if not events:
        print("No pending events.")
        return
    for event in events:
        suffix = f': {event["message"]}' if event.get("message") else ""
        print(f'{event["event"]} {event["key"]}{suffix}')


def command_pending(args: argparse.Namespace) -> int:
    with locked_state() as (state, _):
        refresh_all(state)
        events = visible_events(state, args)
    emit_events(events, args.json)
    return 0


def command_drain(args: argparse.Namespace) -> int:
    with locked_state() as (state, _):
        refresh_all(state)
        events = visible_events(state, args)
        acknowledged = set(state.get("acked_event_ids", []))
        acknowledged.update(event["event_id"] for event in events)
        state["acked_event_ids"] = sorted(acknowledged)
    emit_events(events, args.json)
    return 0


def command_follow(args: argparse.Namespace) -> int:
    seen: set[str] = set()
    if args.follow:
        seen = {event["event_id"] for event in read_events(state_path())}
    try:
        while True:
            with locked_state() as (state, _):
                refresh_all(state)
                events = visible_events(state, args, include_acked=True)
            fresh = [event for event in events if event["event_id"] not in seen]
            for event in fresh:
                print(json.dumps(event, separators=(",", ":")), flush=True)
                seen.add(event["event_id"])
            if args.once or not args.follow:
                return 0
            time.sleep(args.interval)
    except KeyboardInterrupt:
        return 0


def deliver_event(event: dict[str, Any], args: argparse.Namespace) -> None:
    encoded = json.dumps(event, separators=(",", ":"))
    if args.wake_dir:
        directory = Path(args.wake_dir).expanduser()
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f'{event["timestamp"].replace(":", "-")}-{event["event_id"]}.json'
        temporary = target.with_suffix(".tmp")
        temporary.write_text(encoded + "\n", encoding="utf-8")
        temporary.replace(target)
    if args.hook:
        try:
            result = subprocess.run(
                [str(Path(args.hook).expanduser())],
                input=encoded + "\n",
                text=True,
                capture_output=True,
                timeout=args.hook_timeout,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as error:
            raise TimerError("hook_failed", f"hook could not run: {error}") from error
        if result.returncode != 0:
            detail = result.stderr.strip() or f"exit {result.returncode}"
            raise TimerError("hook_failed", f"hook failed: {detail}")


def command_daemon(args: argparse.Namespace) -> int:
    try:
        while True:
            candidates: list[dict[str, Any]] = []
            with locked_state() as (state, _):
                refresh_all(state)
                delivered = set(state.setdefault("daemon_delivered_event_ids", []))
                if args.hook or args.wake_dir:
                    candidates = [
                        event
                        for event in read_events(state_path())
                        if event.get("namespace", "default") == namespace_for(args)
                        and event.get("event") in {"expired", "tick"}
                        and event["event_id"] not in delivered
                    ]
            for event in candidates:
                deliver_event(event, args)
                with locked_state() as (state, _):
                    delivered = set(state.setdefault("daemon_delivered_event_ids", []))
                    delivered.add(event["event_id"])
                    state["daemon_delivered_event_ids"] = sorted(delivered)
            if args.once:
                return 0
            time.sleep(args.poll_interval)
    except KeyboardInterrupt:
        return 0


def command_schema(args: argparse.Namespace) -> int:
    print(json.dumps(schema_document(), indent=None if args.json else 2, separators=(",", ":") if args.json else None))
    return 0


def stopwatch_elapsed(stopwatch: dict[str, Any], now: float | None = None) -> float:
    now = time.time() if now is None else now
    elapsed = float(stopwatch.get("accumulated_seconds", 0.0))
    if stopwatch["status"] == "running":
        elapsed += now - stopwatch["started_at"]
    return max(0, round(elapsed, 3))


def public_stopwatch(stopwatch: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    return {
        "id": stopwatch["id"],
        "label": stopwatch["label"],
        "status": stopwatch["status"],
        "elapsed_seconds": stopwatch_elapsed(stopwatch, now),
        "laps": [dict(lap) for lap in stopwatch.get("laps", [])],
    }


def emit_stopwatch(payload: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, separators=(",", ":")))
        return
    if isinstance(payload, list):
        if not payload:
            print("No stopwatches.")
        for stopwatch in payload:
            print(
                f'{stopwatch["label"]}: {stopwatch["status"]} '
                f'({adaptive_time(stopwatch["elapsed_seconds"])} elapsed)'
            )
        return
    print(
        f'{payload["label"]}: {payload["status"]} '
        f'({adaptive_time(payload["elapsed_seconds"])} elapsed)'
    )


def command_stopwatch_start(args: argparse.Namespace) -> int:
    now = time.time()
    label = args.label_option or args.label or "stopwatch"
    stopwatch = {
        "id": str(uuid.uuid4()),
        "label": label,
        "status": "running",
        "created_at": now,
        "started_at": now,
        "accumulated_seconds": 0.0,
        "laps": [],
    }
    with locked_state() as (state, _):
        active = [item for item in state["stopwatches"] if item["status"] != "stopped"]
        ensure_unique_active_label(stopwatch["label"], active)
        state["stopwatches"].append(stopwatch)
    emit_stopwatch(public_stopwatch(stopwatch, now), args.json)
    return 0


def command_stopwatch_list(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        stopwatches = [public_stopwatch(item, now) for item in state["stopwatches"]]
    if not args.all:
        stopwatches = [item for item in stopwatches if item["status"] != "stopped"]
    if not stopwatches and args.hint_if_empty and not args.json:
        print("No active stopwatches. Start one: timer stopwatch start prep")
        return 0
    emit_stopwatch(stopwatches, args.json)
    return 0


def resolve_stopwatch(state: dict[str, Any], identifier: str | None) -> dict[str, Any]:
    if identifier:
        return find_stopwatch(state, identifier)
    active = [item for item in state["stopwatches"] if item["status"] != "stopped"]
    if not active:
        raise ValueError("no active stopwatch")
    if len(active) > 1:
        raise ValueError(f"multiple stopwatches are active; choose one: {choice_labels(active)}")
    return active[0]


def command_stopwatch_status(args: argparse.Namespace) -> int:
    with locked_state() as (state, _):
        result = public_stopwatch(resolve_stopwatch(state, args.identifier))
    emit_stopwatch(result, args.json)
    return 0


def command_stopwatch_pause(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        stopwatch = resolve_stopwatch(state, args.identifier)
        if stopwatch["status"] != "running":
            raise ValueError("only a running stopwatch can be paused")
        stopwatch["accumulated_seconds"] = stopwatch_elapsed(stopwatch, now)
        stopwatch["status"] = "paused"
        stopwatch["paused_at"] = now
        result = public_stopwatch(stopwatch, now)
    emit_stopwatch(result, args.json)
    return 0


def command_stopwatch_resume(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        stopwatch = resolve_stopwatch(state, args.identifier)
        if stopwatch["status"] != "paused":
            raise ValueError("only a paused stopwatch can be resumed")
        stopwatch["status"] = "running"
        stopwatch["started_at"] = now
        stopwatch.pop("paused_at", None)
        result = public_stopwatch(stopwatch, now)
    emit_stopwatch(result, args.json)
    return 0


def command_stopwatch_lap(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        stopwatch = resolve_stopwatch(state, args.identifier)
        if stopwatch["status"] != "running":
            raise ValueError("laps can only be recorded on a running stopwatch")
        total = stopwatch_elapsed(stopwatch, now)
        previous_total = stopwatch["laps"][-1]["total_seconds"] if stopwatch["laps"] else 0.0
        lap = {
            "number": len(stopwatch["laps"]) + 1,
            "elapsed_seconds": round(total - previous_total, 3),
            "total_seconds": total,
            "recorded_at": datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
        }
        stopwatch["laps"].append(lap)
        append_event(
            state_path(),
            make_event(
                stopwatch,
                "lap",
                timestamp=now,
                extra={"kind": "stopwatch", "lap": dict(lap)},
            ),
        )
        result = public_stopwatch(stopwatch, now)
    emit_stopwatch(result, args.json)
    return 0


def command_stopwatch_reset(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        stopwatch = resolve_stopwatch(state, args.identifier)
        stopwatch["accumulated_seconds"] = 0.0
        stopwatch["laps"] = []
        if stopwatch["status"] == "running":
            stopwatch["started_at"] = now
        result = public_stopwatch(stopwatch, now)
    emit_stopwatch(result, args.json)
    return 0


def command_stopwatch_stop(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        stopwatch = resolve_stopwatch(state, args.identifier)
        if stopwatch["status"] == "running":
            stopwatch["accumulated_seconds"] = stopwatch_elapsed(stopwatch, now)
        if stopwatch["status"] == "stopped":
            raise ValueError("stopwatch is already stopped")
        stopwatch["status"] = "stopped"
        stopwatch["stopped_at"] = now
        stopwatch.pop("paused_at", None)
        result = public_stopwatch(stopwatch, now)
    emit_stopwatch(result, args.json)
    return 0


class TimerArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise TimerError("invalid", message)


def add_scope_arguments(
    command: argparse.ArgumentParser, *, lookup: bool = False, mine: bool = False, mutation: bool = False
) -> None:
    command.add_argument("--namespace", help="isolated timer namespace (or TIMER_NAMESPACE)")
    command.add_argument("--owner", help="timer owner (or TIMER_OWNER)")
    if lookup:
        command.add_argument("--id", help="exact timer ID")
        command.add_argument("--key", help="exact idempotency key")
    if mine:
        command.add_argument("--mine", action="store_true", help="show only events or timers owned by --owner")
    if mutation:
        command.add_argument("--force", action="store_true", help="override owner protection")


def build_parser() -> argparse.ArgumentParser:
    parser = TimerArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Persistent timers and stopwatches for the command line",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a timer")
    start.add_argument("duration", help="duration such as 10m, 1h30m, or 45s")
    start.add_argument("--label", help="human-readable timer label")
    start.add_argument("--key", help="idempotent timer key; also becomes the label when --label is omitted")
    start.add_argument("--message", help="continuation message returned with timer events")
    start.add_argument("--ref", help="opaque task or workflow reference")
    add_scope_arguments(start)
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=command_start)

    listing = subparsers.add_parser("list", help="list timers")
    listing.add_argument("--all", action="store_true", help="include expired and cancelled timers")
    add_scope_arguments(listing, mine=True)
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--hint-if-empty", action="store_true", help=argparse.SUPPRESS)
    listing.set_defaults(func=command_list)

    for name, function in (("status", command_status), ("cancel", command_cancel)):
        command = subparsers.add_parser(name, help=f"{name} a timer")
        command.add_argument("identifier", nargs="?", help="timer label, ID, or unique ID prefix")
        add_scope_arguments(command, lookup=True, mutation=name == "cancel")
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=function)

    rename = subparsers.add_parser("rename", help="rename an active timer")
    rename.add_argument("identifier", nargs="?", help="current timer label, ID, or unique ID prefix")
    rename.add_argument("new_label", nargs="?", help="new unique label")
    rename.add_argument("--new-label", dest="new_label_option", help="new label when selecting with --id or --key")
    add_scope_arguments(rename, lookup=True, mutation=True)
    rename.add_argument("--json", action="store_true")
    rename.set_defaults(func=command_rename)

    wait = subparsers.add_parser("wait", help="wait until a timer expires or is cancelled")
    wait.add_argument("identifier", nargs="?", help="timer label, ID, or unique ID prefix")
    wait_mode = wait.add_mutually_exclusive_group()
    wait_mode.add_argument("--any", dest="any_keys", help="wait for any comma-separated timer keys")
    wait_mode.add_argument("--all", action="store_true", help="wait for all timers matching --key-prefix")
    wait.add_argument("--key-prefix", help="key prefix used with --all")
    add_scope_arguments(wait, lookup=True)
    wait.add_argument("--poll-interval", type=positive_float, default=0.25)
    wait.add_argument("--json", action="store_true")
    wait.set_defaults(func=command_wait)

    watch = subparsers.add_parser("watch", help="watch active timers count down")
    watch.add_argument("--once", action="store_true", help="print one read-only snapshot")
    watch.add_argument("--json", action="store_true", help="emit NDJSON events without terminal control codes")
    watch.add_argument("--follow", action="store_true", help="follow new events; requires --json")
    add_scope_arguments(watch, mine=True)
    watch.add_argument("--interval", type=positive_float, default=1.0, help=argparse.SUPPRESS)
    watch.set_defaults(func=command_watch)

    for name, function, help_text in (
        ("pending", command_pending, "peek at unacknowledged timer events"),
        ("drain", command_drain, "return and acknowledge timer events"),
    ):
        inbox = subparsers.add_parser(name, help=help_text)
        inbox.add_argument("--event", dest="event_type", choices=("started", "expired", "cancelled", "lap", "tick"))
        inbox.add_argument("--json", action="store_true")
        add_scope_arguments(inbox, mine=True)
        inbox.set_defaults(func=function)

    daemon = subparsers.add_parser("daemon", help="materialize expiration events and notify a host")
    daemon.add_argument("--hook", help="executable receiving each expiry event as JSON on stdin")
    daemon.add_argument("--wake-dir", help="directory that receives one JSON file per expiry")
    daemon.add_argument("--once", action="store_true", help="process due timers once and exit")
    daemon.add_argument("--poll-interval", type=positive_float, default=0.25)
    daemon.add_argument("--hook-timeout", type=positive_float, default=30.0)
    daemon.add_argument("--json", action="store_true")
    add_scope_arguments(daemon)
    daemon.set_defaults(func=command_daemon)

    every = subparsers.add_parser("every", help="schedule recurring heartbeat events")
    every.add_argument("interval", help="tick interval such as 2m")
    every.add_argument("--until", required=True, help="maximum schedule duration such as 30m")
    every.add_argument("--key", required=True, help="idempotent recurring schedule key")
    every.add_argument("--label", help="human-readable label")
    every.add_argument("--message", help="continuation message returned with each tick")
    every.add_argument("--ref", help="opaque task or workflow reference")
    every.add_argument("--json", action="store_true")
    add_scope_arguments(every)
    every.set_defaults(func=command_every)

    schema = subparsers.add_parser("schema", help="print the stable machine-readable contract")
    schema.add_argument("--json", action="store_true")
    schema.set_defaults(func=command_schema)

    stopwatch = subparsers.add_parser("stopwatch", help="control persistent stopwatches")
    stopwatch_commands = stopwatch.add_subparsers(dest="stopwatch_command", required=True)

    stopwatch_start = stopwatch_commands.add_parser("start", help="start a stopwatch")
    stopwatch_start.add_argument("label", nargs="?", help="human-readable stopwatch label")
    stopwatch_start.add_argument("--label", dest="label_option", help="advanced label form")
    stopwatch_start.add_argument("--json", action="store_true")
    stopwatch_start.set_defaults(func=command_stopwatch_start)

    stopwatch_list = stopwatch_commands.add_parser("list", help="list stopwatches")
    stopwatch_list.add_argument("--all", action="store_true", help="include stopped stopwatches")
    stopwatch_list.add_argument("--json", action="store_true")
    stopwatch_list.add_argument("--hint-if-empty", action="store_true", help=argparse.SUPPRESS)
    stopwatch_list.set_defaults(func=command_stopwatch_list)

    stopwatch_actions = (
        ("status", command_stopwatch_status),
        ("pause", command_stopwatch_pause),
        ("resume", command_stopwatch_resume),
        ("lap", command_stopwatch_lap),
        ("reset", command_stopwatch_reset),
        ("stop", command_stopwatch_stop),
    )
    for name, function in stopwatch_actions:
        command = stopwatch_commands.add_parser(name, help=f"{name} a stopwatch")
        command.add_argument("identifier", nargs="?", help="stopwatch label, ID, or unique ID prefix")
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=function)
    return parser


def normalize_argv(argv: list[str]) -> list[str]:
    """Translate the terse user interface into the stable command grammar."""
    if not argv:
        return ["list", "--hint-if-empty"]
    if argv[0] in ("-h", "--help"):
        return argv

    if argv[0] == "stopwatch":
        if len(argv) == 1:
            return ["stopwatch", "list", "--hint-if-empty"]
        if len(argv) >= 2 and argv[1] not in STOPWATCH_COMMANDS:
            return ["stopwatch", "status", *argv[1:]]
        return argv

    if argv[0] in STOPWATCH_SHORTCUTS:
        return ["stopwatch", argv[0], *argv[1:]]

    if argv[0] in TOP_LEVEL_COMMANDS:
        return argv

    try:
        parse_duration(argv[0])
    except ValueError:
        return ["status", *argv]

    duration = argv[0]
    rest = argv[1:]
    option_index = next((index for index, value in enumerate(rest) if value.startswith("-")), len(rest))
    label_parts = rest[:option_index]
    options = rest[option_index:]
    label = " ".join(label_parts) if label_parts else f"{duration} timer"
    return ["start", duration, "--label", label, *options]


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than zero")
    return number


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    argv = normalize_argv(argv)
    parser = build_parser()
    as_json = "--json" in argv
    try:
        args = parser.parse_args(argv)
        if getattr(args, "follow", False) and not getattr(args, "json", False):
            raise TimerError("invalid", "--follow requires --json")
        return args.func(args)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        message = error.args[0] if error.args else str(error)
        if isinstance(error, TimerError):
            code = error.code
        elif isinstance(error, KeyError):
            code = "not_found"
        elif "ambiguous" in str(message).lower() or "multiple" in str(message).lower():
            code = "ambiguous"
        elif "already active" in str(message).lower():
            code = "conflict"
        else:
            code = "invalid"
        if as_json:
            print(json.dumps({
                "ok": False,
                "schema": TIMER_SCHEMA,
                "event": "error",
                "error": {"code": code, "message": str(message)},
            }, separators=(",", ":")))
            return 0
        print(f"{parser.prog}: {message}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
