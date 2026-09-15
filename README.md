# Agent Timer

Agent Timer is a Python 3.10+ local timer and stopwatch CLI designed for tools such as Codex. It has no third-party runtime dependencies. State is persistent, so timers and stopwatches survive terminal interruptions and Mac sleep. A waiting command completes when a timer expires, which gives an agent a direct wake-up event.

## Five useful commands

```sh
timer 10m rice
timer 30s
timer list
timer rice
timer cancel rice
```

The first command starts a labeled ten-minute timer. The second starts an unlabeled timer and generates the label `30s timer`. A label by itself shows that timer's status. Durations accept combinations such as `45s`, `10m`, and `1h30m`; a bare integer means seconds.

For a live view, run:

```sh
timer watch
```

It refreshes active countdowns about once per second, exits when none remain, and handles Control-C cleanly. `timer watch --once` prints one read-only snapshot.

Timer output preserves the largest unit originally entered: `30s`, `15m 00s`, or `1h 05m 00s`. A 15-minute timer therefore shows `0m 59s` near its end. Decimal seconds and internal IDs are hidden in normal output; `--json` retains exact numeric seconds and stable IDs for agents and scripts.

Running timer labels are unique without regard to capitalization. A duplicate is refused with a suggested label such as `peppers-2`; expired and cancelled timers release their labels. Rename a running timer without changing its deadline:

```sh
timer rename peppers-1 peppers
```

Full IDs and unique ID prefixes work anywhere an explicit identifier is accepted. `list` shows active timers by default; use `list --all` to include expired and cancelled timers.

Bare commands provide safe defaults:

- `timer` shows active timers or one short example when none exist.
- `timer status` shows the sole active timer or all active timers.
- `timer cancel` cancels automatically only when exactly one timer is active; with multiple timers it refuses and lists their labels.
- `timer stopwatch` shows active stopwatches or one short start example.

Multiple labeled timers run independently:

```sh
timer 25m peppers
timer 10m rice
timer list
timer peppers
timer wait rice --json
timer cancel peppers
```

Waiting for one timer does not block another timer from being listed, checked, or cancelled by a separate command.

## Stopwatches

Stopwatches persist their elapsed time, status, and laps across commands:

```sh
timer stopwatch start prep
timer stopwatch prep
timer lap
timer pause
timer resume
timer reset
timer stopwatch stop prep
timer stopwatch list --all
```

`status` reports the current elapsed time. `pause` freezes it, `resume` continues from that elapsed time, `lap` records both the lap duration and total duration, `reset` clears elapsed time and laps while preserving whether the stopwatch is running or paused, and `stop` finalizes it. The top-level shortcuts infer the target only when exactly one stopwatch is active; otherwise they refuse and list choices. Timer and stopwatch labels use separate namespaces. Add `--json` to any stopwatch command for structured output.

## Install

Clone the repository, then install it for your user:

```sh
git clone https://github.com/tszaks/agent-timer.git
cd agent-timer
python3 -m pip install --user .
```

The primary command is `timer`. The older `agent-timer` command remains available as a backwards-compatible advanced alias. You can also run the checked-out `./agent-timer` launcher directly without installing.

The original explicit grammar remains supported when an agent needs it:

```sh
agent-timer start 10m --label rice --json
agent-timer status rice --json
```

## How Codex uses the wake-up

1. Start the timer and retain its returned ID.
2. Run `timer wait ID --json` as a long-running command.
3. Yield the command session while other work continues.
4. When the command completes, the JSON event tells Codex that the timer expired.

The `wait` process does not own the timer. The timestamp remains in the state file, so another process can inspect or cancel it at any time. By default, timer and stopwatch state is stored in `~/.local/share/agent-timer/timers.json`; set `AGENT_TIMER_STATE` to choose another location.

`wait` exits with status 0 after expiration. If the timer is cancelled, it prints the cancelled timer and exits with status 2, allowing an agent to distinguish cancellation from a wake-up.

## Current boundary

This first version wakes an agent that is already waiting on the command. Waking a fully closed Codex task requires a separate Codex scheduler or heartbeat bridge; that integration is deliberately not claimed here.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
