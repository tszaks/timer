# Timer

[![Tests](https://github.com/tszaks/timer/actions/workflows/test.yml/badge.svg)](https://github.com/tszaks/timer/actions/workflows/test.yml)

Timer is a persistent timer, stopwatch, and deferred-work CLI for Unix systems. Version 0.5.0 requires Python 3.10 or newer and has no third-party runtime dependencies.

Timer stores absolute deadlines on disk. Timers and stopwatches survive terminal exits and system sleep. An expiration is processed the next time a Timer command refreshes state, or shortly after its deadline when the optional daemon is running.

Timer does not play a sound or display a desktop notification by itself. It can print countdowns, write expiration files, run a hook, or queue a new Codex turn through the included supervisor.

## Install

Clone the repository and install it with `pipx`:

```sh
git clone https://github.com/tszaks/timer.git
cd timer
pipx install .
```

This installs two commands:

- `timer` manages timers, stopwatches, events, and delivery.
- `timer-supervisor` converts a Timer event into a queued Codex turn.

You can also run `./timer` directly from the repository without installing it.

## Start a timer

```sh
timer 10m rice
timer 25m peppers
timer list
timer rice
timer cancel peppers
```

The short form is designed for people:

```text
timer DURATION [LABEL]
```

Durations accept hours, minutes, seconds, and combinations:

```sh
timer 45s tea
timer 10m rice
timer 1h30m laundry
timer 90 stretch
```

A timer without a label gets one from its duration, such as `30s timer`. Active labels are unique without regard to capitalization. Expired and cancelled timers release their labels.

Useful commands:

| Command | Result |
| --- | --- |
| `timer` | List active timers |
| `timer LABEL` | Show one timer |
| `timer list --all` | Include expired and cancelled timers |
| `timer rename OLD NEW` | Rename an active timer without changing its deadline |
| `timer cancel LABEL` | Cancel a timer |
| `timer wait LABEL` | Block until a timer expires or is cancelled |
| `timer watch` | Show a live, read-only countdown |

Full IDs and unique ID prefixes can replace labels where an identifier is accepted. If exactly one timer is active, `timer status` and `timer cancel` can infer it. With multiple active timers, Timer requires an explicit choice.

`timer watch` does not write state or materialize expiration events. It exits when no timers remain active in its view. Use `timer wait`, another state-refreshing command, or the daemon when a durable expiration event is required.

## Stopwatches

Stopwatches persist their elapsed time, status, and laps:

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

The short `lap`, `pause`, `resume`, and `reset` commands infer the target only when one stopwatch is active. Timer and stopwatch labels use separate namespaces.

## JSON commands

Scripts and agents should use the explicit command form and `--json`:

```sh
timer start 10m --label rice --json
timer status rice --json
timer cancel rice --json
```

With `--json`, expected domain errors also return structured JSON and exit with status 0. Check the top-level `ok` field. Human-mode errors exit nonzero.

Print the current command, event, and lease schemas with:

```sh
timer schema --json
```

## Durable work for agents

Use a keyed timer when an agent needs to continue work later:

```sh
timer start 10m \
  --key rice-check \
  --namespace agent:cook \
  --owner codex \
  --message "Check whether the rice is done." \
  --ref thread:THREAD_ID \
  --payload '{"action":"inspect_rice","retry":"3m"}' \
  --json
```

`--key` makes an active timer idempotent. Repeating the same key and duration returns the existing timer. Reusing the key with a different duration returns a `conflict` error.

`--payload` must be a JSON object. `--payload-file PATH` reads the same object from a file. Route references must use one of these forms:

- `thread:<id>` routes directly to a Codex thread.
- `session:<id>` routes to a Codex session through the same queue interface.
- `task:<id>` names logical work. To use the Codex supervisor, include `"route_ref":"thread:<id>"` or `"route_ref":"session:<id>"` in the payload.

### Claim, work, acknowledge

Expiration events are stored in `events.jsonl`. A worker leases one event from its own consumer cursor:

```sh
timer claim \
  --consumer codex:cook \
  --namespace agent:cook \
  --owner codex \
  --mine \
  --event expired \
  --lease 2m \
  --json
```

A successful claim returns `event: "claimed"`, a `delivery` object, a stable `delivery.event_id`, and a unique `lease_id`. It returns `busy` while that consumer has an active lease and `empty` when no matching event is available.

After the work succeeds:

```sh
timer ack EVENT_ID \
  --consumer codex:cook \
  --lease-id LEASE_ID \
  --json
```

If the work cannot begin safely, release it immediately:

```sh
timer nack EVENT_ID \
  --consumer codex:cook \
  --lease-id LEASE_ID \
  --json
```

If a worker exits without either command, the event becomes claimable again after the lease expires. The `lease_id` prevents a stale worker from acknowledging or releasing a newer claim.

Delivery is at least once. Use `delivery.event_id` as an idempotency key when the downstream action must happen once.

Each new consumer starts at the beginning of the retained event log. Each consumer name owns one cursor and one fixed filter set. Reusing a consumer with a different namespace, owner filter, or event filter returns `consumer_conflict`. Separate delivery paths need separate consumer names. Workers that should compete for the same queue share one consumer name.

### Inspect or drain events

`pending` reads available events without advancing its consumer cursor:

```sh
timer pending --consumer inspector --event expired --json
```

`drain` returns and acknowledges all matching events:

```sh
timer drain --consumer simple-script --event expired --json
```

Do not use `drain` for work that must survive a crash between receipt and completion. Use `claim`, then `ack` or `nack`.

Without `--consumer`, `pending` and `drain` use a compatibility cursor derived from their namespace, owner, and event filter.

## Wait for timers or follow events

```sh
timer wait --key rice-check --json
timer wait --any rice,beans --json
timer wait --all --key-prefix cook- --json
```

`--any` snapshots the listed keys and returns the first terminal event. `--all` snapshots the latest timer for each matching key and returns when every timer is terminal.

Follow events appended after the command starts as newline-delimited JSON:

```sh
timer watch --json --follow
```

This mode contains no screen-clearing control codes. It also refreshes timer and recurring-schedule state while it runs.

## Daemon and delivery hooks

The optional daemon runs in the foreground. It materializes due timer and recurring events and can deliver each `expired` or `tick` event to an executable hook, a wake directory, or both:

```sh
timer daemon
timer daemon --hook /absolute/path/to/hook
timer daemon --wake-dir /absolute/path/to/wake-directory
timer daemon --hook /absolute/path/to/hook --wake-dir /absolute/path/to/wake-directory
```

Hooks run directly without a shell and receive one raw `timer.event.v1` JSON object on standard input. Wake files are written atomically, one file per event. Hook and wake-directory delivery use independent consumer cursors, so one path does not consume the other path's event. A new hook path or wake-directory path receives matching events still retained in the log. A failed path is released for retry after the other path has had a chance to run.

Run one refresh pass and exit with:

```sh
timer daemon --once --wake-dir /absolute/path/to/wake-directory
```

Installing Timer does not start the daemon. The repository contains service templates for [launchd](examples/launchd/com.tszaks.timer-supervisor.plist) and [systemd](examples/systemd/timer-supervisor.service). Replace their placeholders and paths before installing them.

## Codex supervisor

`timer-supervisor` reads one raw Timer event from standard input and queues a new Codex turn with `codex queue`. The event needs a `thread:` or `session:` route, either in `ref` or in `payload.route_ref`.

Connect it to the daemon:

```sh
timer daemon --hook "$(command -v timer-supervisor)"
```

Inspect the generated continuation envelope without contacting Codex:

```sh
printf '%s\n' '{"schema":"timer.event.v1","event_id":"demo-event","event":"expired","id":"demo-timer","key":"rice-check","timestamp":"2026-01-01T12:00:00Z","ref":"thread:THREAD_ID","payload":{"action":"inspect_rice"}}' \
  | timer-supervisor --dry-run
```

The supervisor requires the Codex CLI for live delivery. Timer itself remains host-neutral, so another host can consume the same event schema through a different hook.

The repository also contains an optional [Timer agent skill](skills/timer-agent/SKILL.md) with the claim and acknowledgement protocol. Installing the Python package does not install or enable that skill automatically.

## Recurring schedules

Create a bounded recurring schedule with `every`:

```sh
timer every 2m \
  --until 30m \
  --key deploy-check \
  --message "Read deployment status. Act only if it failed." \
  --ref thread:THREAD_ID \
  --json
```

The schedule emits durable `tick` events until its `--until` window ends. Cancel it with:

```sh
timer cancel --key deploy-check
```

Recurring ticks are materialized by the daemon, `claim`, `pending`, `drain`, JSON event following, or multi-timer waits. If several ticks are overdue, one refresh emits at most 100 and a later refresh continues the catch-up.

## Namespaces and ownership

Namespaces separate timers that use the same key or label. Owners protect cancellation and renaming:

```sh
timer start 10m --key rice --namespace agent:cook --owner codex --json
timer list --namespace agent:cook --owner codex --mine --json
timer cancel --key rice --namespace agent:cook --owner codex --json
```

`TIMER_NAMESPACE` and `TIMER_OWNER` set defaults. `cancel` and `rename` require the matching owner unless `--force` is supplied.

## Storage and compaction

Default storage:

| Data | Path |
| --- | --- |
| Timer, stopwatch, and recurring state | `~/.local/share/timer/timers.json` |
| Event log | `~/.local/share/timer/events.jsonl` |
| Consumer cursors and leases | `~/.local/share/timer/consumers.json` |

Override the paths with `TIMER_STATE`, `TIMER_EVENTS`, and `TIMER_CONSUMERS`. When `TIMER_EVENTS` points to a shared log, state files use the same default consumer registry so compaction sees every registered consumer.

Remove the event-log prefix consumed by every registered consumer:

```sh
timer compact --json
```

Compaction stops at the oldest cursor. It also removes terminal timer and recurring-schedule records whose terminal events are safely inside the consumed prefix, plus stopped stopwatch records when events are removed. A journal repairs cursor offsets if compaction is interrupted after replacing the log. A registered consumer that never advances will prevent older events from being removed.

## Development

Run the test suite:

```sh
python3 -m unittest discover -s tests -v
```

Build the wheel and source archive with [uv](https://docs.astral.sh/uv/):

```sh
uv build
```

The GitHub workflow tests Python 3.10 and 3.14 on macOS.

## License

MIT. See [LICENSE](LICENSE).
