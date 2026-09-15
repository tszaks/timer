# Timer

Timer is a Python 3.10+ local timer and stopwatch CLI. It has no third-party runtime dependencies. State is persistent, so timers and stopwatches survive terminal interruptions and Mac sleep. A waiting command completes when a timer expires, which also makes it useful for command-line agents such as Codex.

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

Clone the repository, then install it as an isolated command-line app with
[`pipx`](https://pipx.pypa.io/):

```sh
git clone https://github.com/tszaks/timer.git
cd timer
pipx install .
```

The command is `timer`. You can also run the checked-out `./timer` launcher directly without installing.

The explicit grammar is useful for scripts and agents:

```sh
timer start 10m --label rice --json
timer status rice --json
```

## Deferred messages for agents

A keyed timer can carry a continuation message and an opaque reference:

```sh
timer start 10m --key rice --json \
  --message "Check the pot. If still wet, start another 3m timer. If done, plate it." \
  --ref task:cook-1
```

`--key` makes start idempotent. Retrying the same active key and duration returns the original timer, including its original message and deadline. Reusing that active key with a different duration returns a structured `conflict` error. Ordinary human labels keep their original duplicate-rejection behavior.

When the timer expires, its continuation is preserved in an append-only `events.jsonl` inbox. Peek without changing it, or drain and acknowledge the returned events:

```sh
timer pending --event expired --json
timer drain --event expired --json
```

An expiration event has a stable `timer.event.v1` schema:

```json
{"ok":true,"schema":"timer.event.v1","event":"expired","key":"rice","message":"Check the pot. If still wet, start another 3m timer. If done, plate it.","ref":"task:cook-1"}
```

The real event also includes its event ID, timer ID, timestamp, namespace, owner, deadline, status, and remaining seconds. `pending` peeks; `drain` acknowledges only the events it returns.

## Waiting and streaming

The original blocking wait remains available:

```sh
timer wait --key rice --json
timer wait --any rice,beans --json
timer wait --all --key-prefix cook- --json
```

`--any` snapshots the listed keys and returns the first terminal event. `--all` snapshots the latest timer for every matching key and returns when all are terminal.

For hosts that support a long-running reader, follow new events as NDJSON with no screen-clearing control codes:

```sh
timer watch --json --follow
```

Human `timer watch` keeps its live countdown display.

## Daemon and host wake-up

`timer daemon` is an optional foreground clock. It materializes due events even when no agent is polling. The host can receive each expiry through an executable hook, wake files, or both:

```sh
timer daemon --hook /path/to/wakeup.sh
timer daemon --wake-dir /path/to/watched-directory
```

Hooks are executed directly without a shell and receive one JSON event on standard input. Wake files are written atomically. `timer daemon --once` is useful for schedulers and tests. Timer deliberately does not contain Codex-, Claude-, or framework-specific resume logic; the host decides how an event resumes an agent.

## Namespaces and ownership

Isolate agents sharing one machine:

```sh
timer start 10m --key rice --namespace agent:codex-123 --owner cook --json
timer list --namespace agent:codex-123 --mine --owner cook --json
timer cancel --key rice --namespace agent:codex-123 --owner cook --json
```

`TIMER_NAMESPACE` and `TIMER_OWNER` provide defaults. Cancel and rename enforce ownership unless `--force` is explicitly supplied. `TIMER_STATE` selects a different state file, and `TIMER_EVENTS` selects a different event log.

## Recurring heartbeats

Bounded recurring schedules emit a durable `tick` event at each interval:

```sh
timer every 2m --until 30m --key deploy \
  --message "Read deployment status. If pending, do nothing. If failed, inspect logs."
timer cancel --key deploy
```

Schedules are persisted, idempotent by key, capped by a required `--until`, and advanced by `daemon`, `pending`, or `drain`.

## Machine-readable behavior

With `--json`, expected domain outcomes always exit 0 and put success or failure in the object. Errors use codes such as `conflict`, `not_found`, `ambiguous`, and `owner_mismatch`. JSON is printed only on standard output. Human-mode errors still exit nonzero.

```sh
timer schema --json
```

This prints the current `timer.v1` command and `timer.event.v1` event contracts.

## Persistence and recovery

The `wait` process does not own a timer. Absolute deadlines, recurring schedules, inbox acknowledgements, and ownership metadata persist on disk, so another process or later session can recover them. By default, state is stored in `~/.local/share/timer/timers.json` and events in the adjacent `events.jsonl` file. The daemon is optional for ordinary start, list, cancel, pending, and drain operations; it is the push bridge when a host needs proactive delivery.

## Tests

```sh
python3 -m unittest discover -s tests -v
```
