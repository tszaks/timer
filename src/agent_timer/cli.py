from __future__ import annotations

import argparse
import fcntl
import json
import math
import os
import re
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator


DURATION_RE = re.compile(r"(?i)(\d+(?:\.\d+)?)(h|m|s)")
TOP_LEVEL_COMMANDS = {"start", "list", "status", "cancel", "rename", "wait", "watch", "stopwatch", "-h", "--help"}
STOPWATCH_COMMANDS = {"start", "list", "status", "pause", "resume", "lap", "reset", "stop", "-h", "--help"}
STOPWATCH_SHORTCUTS = {"lap", "pause", "resume", "reset"}


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
    override = os.environ.get("AGENT_TIMER_STATE")
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME")
    base = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return base / "agent-timer" / "timers.json"


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
        return {"timers": [], "stopwatches": []}
    with path.open("r", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH)
        raw = handle.read()
    state = json.loads(raw) if raw else {}
    state.setdefault("timers", [])
    state.setdefault("stopwatches", [])
    return state


def refresh(timer: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    if timer["status"] == "active" and now >= timer["due_at"]:
        timer["status"] = "expired"
        timer["expired_at"] = timer["due_at"]
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


def find_timer(state: dict[str, Any], identifier: str) -> dict[str, Any]:
    exact = [timer for timer in state["timers"] if timer["id"] == identifier]
    if exact:
        return exact[0]
    label_matches = [
        timer
        for timer in state["timers"]
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
    return find_item(state["timers"], identifier, "timer")


def find_stopwatch(state: dict[str, Any], identifier: str) -> dict[str, Any]:
    return find_item(state["stopwatches"], identifier, "stopwatch")


def public_timer(timer: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    timer = refresh(dict(timer), now)
    original_seconds = timer["due_at"] - timer["created_at"]
    display_unit = timer.get("display_unit") or inferred_unit(original_seconds)
    return {
        "id": timer["id"],
        "label": timer["label"],
        "status": timer["status"],
        "remaining_seconds": timer["remaining_seconds"],
        "display_unit": display_unit,
        "due_at": datetime.fromtimestamp(timer["due_at"]).astimezone().isoformat(timespec="seconds"),
    }


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


def command_start(args: argparse.Namespace) -> int:
    duration = parse_duration(args.duration)
    now = time.time()
    timer = {
        "id": str(uuid.uuid4()),
        "label": args.label,
        "status": "active",
        "created_at": now,
        "due_at": now + duration,
        "display_unit": duration_unit(args.duration),
    }
    with locked_state() as (state, _):
        for item in state["timers"]:
            refresh(item, now)
        active = [item for item in state["timers"] if item["status"] == "active"]
        ensure_unique_active_label(timer["label"], active)
        state["timers"].append(timer)
    emit(public_timer(timer, now), args.json)
    return 0


def command_list(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        for timer in state["timers"]:
            refresh(timer, now)
        timers = [public_timer(timer, now) for timer in state["timers"]]
    if not args.all:
        timers = [timer for timer in timers if timer["status"] == "active"]
    if not timers and args.hint_if_empty and not args.json:
        print("No active timers. Start one: timer 10m rice")
        return 0
    emit(timers, args.json)
    return 0


def command_status(args: argparse.Namespace) -> int:
    with locked_state() as (state, _):
        if args.identifier:
            timer = find_timer(state, args.identifier)
            refresh(timer)
            result: Any = public_timer(timer)
        else:
            now = time.time()
            for timer in state["timers"]:
                refresh(timer, now)
            active = [public_timer(timer, now) for timer in state["timers"] if timer["status"] == "active"]
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
            refresh(item, now)
        if args.identifier:
            timer = find_timer(state, args.identifier)
        else:
            active = [item for item in state["timers"] if item["status"] == "active"]
            if not active:
                raise ValueError("no active timer to cancel")
            if len(active) > 1:
                raise ValueError(f"multiple timers are active; choose one: {choice_labels(active)}")
            timer = active[0]
        if timer["status"] == "active":
            timer["status"] = "cancelled"
            timer["cancelled_at"] = time.time()
        result = public_timer(timer)
    emit(result, args.json)
    return 0


def command_rename(args: argparse.Namespace) -> int:
    now = time.time()
    with locked_state() as (state, _):
        for item in state["timers"]:
            refresh(item, now)
        timer = find_timer(state, args.identifier)
        if timer["status"] != "active":
            raise ValueError("only an active timer can be renamed")
        active = [item for item in state["timers"] if item["status"] == "active"]
        ensure_unique_active_label(args.new_label, active, exclude_id=timer["id"])
        timer["label"] = args.new_label
        result = public_timer(timer, now)
    emit(result, args.json)
    return 0


def command_wait(args: argparse.Namespace) -> int:
    identifier = args.identifier
    while True:
        with locked_state() as (state, _):
            timer = find_timer(state, identifier)
            identifier = timer["id"]
            refresh(timer)
            result = public_timer(timer)
        if result["status"] != "active":
            emit(result, args.json)
            return 0 if result["status"] == "expired" else 2
        time.sleep(min(args.poll_interval, max(0.05, result["remaining_seconds"])))


def command_watch(args: argparse.Namespace) -> int:
    interactive = sys.stdout.isatty()
    try:
        while True:
            now = time.time()
            state = read_state()
            timers = [public_timer(timer, now) for timer in state["timers"]]
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Persistent timers and stopwatches for command-line agents",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    start = subparsers.add_parser("start", help="start a timer")
    start.add_argument("duration", help="duration such as 10m, 1h30m, or 45s")
    start.add_argument("--label", required=True, help="human-readable timer label")
    start.add_argument("--json", action="store_true")
    start.set_defaults(func=command_start)

    listing = subparsers.add_parser("list", help="list timers")
    listing.add_argument("--all", action="store_true", help="include expired and cancelled timers")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--hint-if-empty", action="store_true", help=argparse.SUPPRESS)
    listing.set_defaults(func=command_list)

    for name, function in (("status", command_status), ("cancel", command_cancel)):
        command = subparsers.add_parser(name, help=f"{name} a timer")
        command.add_argument("identifier", nargs="?", help="timer label, ID, or unique ID prefix")
        command.add_argument("--json", action="store_true")
        command.set_defaults(func=function)

    rename = subparsers.add_parser("rename", help="rename an active timer")
    rename.add_argument("identifier", help="current timer label, ID, or unique ID prefix")
    rename.add_argument("new_label", help="new unique label")
    rename.add_argument("--json", action="store_true")
    rename.set_defaults(func=command_rename)

    wait = subparsers.add_parser("wait", help="wait until a timer expires or is cancelled")
    wait.add_argument("identifier", help="timer label, ID, or unique ID prefix")
    wait.add_argument("--poll-interval", type=positive_float, default=0.25)
    wait.add_argument("--json", action="store_true")
    wait.set_defaults(func=command_wait)

    watch = subparsers.add_parser("watch", help="watch active timers count down")
    watch.add_argument("--once", action="store_true", help="print one read-only snapshot")
    watch.add_argument("--interval", type=positive_float, default=1.0, help=argparse.SUPPRESS)
    watch.set_defaults(func=command_watch)

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
    label_parts = [value for value in rest if not value.startswith("-")]
    options = [value for value in rest if value.startswith("-")]
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
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except (ValueError, KeyError, json.JSONDecodeError) as error:
        message = error.args[0] if error.args else str(error)
        print(f"{parser.prog}: {message}", file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
