from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from typing import Any

ENVELOPE_SCHEMA = "timer.next-turn.v1"


def routing_ref(event: dict[str, Any]) -> tuple[str, str]:
    payload = event.get("payload") or {}
    if not isinstance(payload, dict):
        raise TypeError("event payload must be a JSON object")
    for candidate in (event.get("ref"), payload.get("route_ref")):
        if candidate is None:
            continue
        if not isinstance(candidate, str) or not candidate:
            raise ValueError("route references must be non-empty strings")
        for kind in ("session", "thread"):
            prefix = f"{kind}:"
            if candidate.startswith(prefix):
                route_id = candidate[len(prefix):]
                if not route_id:
                    raise ValueError("route references must include an ID")
                return kind, route_id
    for kind in ("session", "thread"):
        route_id = payload.get(kind)
        if route_id is None:
            continue
        if not isinstance(route_id, str) or not route_id:
            raise ValueError("route IDs must be non-empty strings")
        return kind, route_id
    raise ValueError(
        "event needs ref session:<id> or thread:<id>; a task:<id> event may put route_ref in its payload"
    )


def next_turn_envelope(event: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(event, dict):
        raise TypeError("stdin must contain one timer.event.v1 JSON object")
    if event.get("schema") != "timer.event.v1":
        raise ValueError("stdin must contain one timer.event.v1 event")
    route_kind, route_id = routing_ref(event)
    return {
        "schema": ENVELOPE_SCHEMA,
        "source": "timer",
        "event_id": event["event_id"],
        "event": event["event"],
        "route": {"kind": route_kind, "id": route_id},
        "ref": event.get("ref"),
        "message": event.get("message") or f'Timer event "{event["key"]}" is ready.',
        "work": event.get("payload") or {},
        "timer": {
            key: event[key]
            for key in ("id", "key", "timestamp", "due_at", "namespace", "owner")
            if key in event
        },
    }


def codex_message(envelope: dict[str, Any]) -> str:
    return f'{envelope["message"]}\n\nTimer continuation envelope:\n{json.dumps(envelope, separators=(",", ":"))}'


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="timer-supervisor",
        description="Inject a Timer continuation event into a Codex task",
    )
    parser.add_argument("--dry-run", action="store_true", help="print the next-turn envelope without queueing it")
    parser.add_argument("--codex-bin", default=os.environ.get("TIMER_CODEX_BIN", "codex"))
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        event = json.load(sys.stdin)
        envelope = next_turn_envelope(event)
        if args.dry_run:
            print(json.dumps(envelope, separators=(",", ":")))
            return 0
        result = subprocess.run(
            [args.codex_bin, "queue", "--thread", envelope["route"]["id"], "--message", codex_message(envelope)],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
            raise RuntimeError(f"Codex queue failed: {detail}")
        print(json.dumps({
            "ok": True,
            "schema": "timer.supervisor.v1",
            "event": "queued",
            "event_id": envelope["event_id"],
            "thread": envelope["route"]["id"],
        }, separators=(",", ":")))
        return 0
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, OSError, RuntimeError) as error:
        print(f"timer-supervisor: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
