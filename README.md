# Timer

[![Tests](https://github.com/tszaks/timer/actions/workflows/test.yml/badge.svg)](https://github.com/tszaks/timer/actions/workflows/test.yml)

Timer is a persistent timer, stopwatch, and deferred-work CLI for Unix systems. Version 0.8.0 requires Python 3.10 or newer and has no third-party runtime dependencies.

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

To validate and install the Codex wake service for your user account:

```sh
timer setup --dry-run
timer setup
timer service status
```

Setup supports macOS launchd and Linux systemd user services. It refuses cleanly if `codex queue` or `timer-supervisor` is unavailable. It also parses `codex queue --help` and requires the installed `--thread` flag to accept session UUIDs or exact session names. Before writing or starting a service, it sends one synthetic expiry through `timer-supervisor --dry-run`. The synthetic expiry never enters the Codex queue. After activation, setup verifies that the service manager reports the daemon as running.

The installed service starts its consumer at the current end of the retained log. Old events are not replayed during first-time setup. If that exact consumer already exists, setup preserves its cursor.

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
| `timer` | List active timers and recurring schedules |
| `timer LABEL` | Show one timer or recurring schedule |
| `timer list --all` | Include terminal timers and recurring schedules |
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

Agents should use one recipe: start a keyed timer, claim one expiry, complete the work, then acknowledge that exact lease.

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
- `session:<id>` routes through `codex queue --thread`, whose argument accepts a session UUID or exact session name.
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

`drain` is for simple scripts. It returns and acknowledges all matching events before the caller starts work:

```sh
timer drain --consumer simple-script --event expired --json
```

Do not use `drain` for agent work or any work that must survive a crash between receipt and completion. Use `claim`, then `ack` or `nack`.

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

Hooks run directly without a shell and receive one raw `timer.event.v1` JSON object on standard input. Wake files are written atomically, one file per event. Hook and wake-directory delivery use independent consumer cursors, so one path does not consume the other path's event. A new hook path or wake-directory path receives matching events still retained in the log.

A hook failure records its code, message, event ID, time, and consecutive-failure count on that hook's consumer. It releases the lease and keeps the daemon alive. After five failures on the same event, that consumer dead-letters the event by advancing only its own cursor; other consumers can still claim it. The daemon logs one `dropped` line and continues with later events. Change the retry bound with `--max-hook-failures N`.

Only one daemon can run for a state file and namespace. A second process exits with a `busy` error and reports the first process ID when available.

Run one refresh pass and exit with:

```sh
timer daemon --once --wake-dir /absolute/path/to/wake-directory
```

Installing the Python package does not start the daemon. Run `timer setup` to generate and activate a user service. The repository also contains reference templates for [launchd](examples/launchd/com.tszaks.timer-supervisor.plist) and [systemd](examples/systemd/timer-supervisor.service).

## Codex supervisor

`timer-supervisor` reads one raw Timer event from standard input and queues a new Codex turn with `codex queue`. The event needs a `thread:` or `session:` route, either in `ref` or in `payload.route_ref`. Both route kinds use the installed `codex queue --thread` argument because that argument explicitly accepts session UUIDs and exact session names; setup refuses installation when that capability is absent.

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
  --max-unacked 3 \
  --key deploy-check \
  --message "Read deployment status. Act only if it failed." \
  --ref thread:THREAD_ID \
  --json
```

The schedule emits durable `tick` events until its `--until` window ends. Bare `timer`, `timer list`, and `timer status` include active or backlog-paused schedules, so a repeating job is visible on the default status surface. Cancel it with:

```sh
timer cancel --key deploy-check
```

Recurring ticks are materialized by the daemon, `claim`, `pending`, `drain`, JSON event following, or multi-timer waits. By default, Timer pauses a series after three ticks remain unacknowledged. `--max-unacked N` changes that bound. A successful acknowledgement by any consumer lowers the backlog; the next refresh resumes the series from the present instead of replaying every missed interval. This is a safety brake, while `--until` is only the schedule's maximum lifetime.

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

Inspect registered consumers and remove one that is no longer used:

```sh
timer consumers
timer forget NAME
```

`timer consumers --json` reports each exact name, filter set, lease state and age, leased event ID, cursor, retained-log lag, last delivery error and age, consecutive failures, last progress time, and consumer-local skipped-event count. `timer forget` refuses to remove a consumer with an active lease unless `--force` is supplied. Reusing a forgotten name later creates a new consumer at the beginning of the retained log.

`timer service status` combines the service-manager process result with the installed hook consumer. It reports `event: "delivering"` only when the process is running, lag is zero or shrinking, no lease is older than the delivery-lease bound, and consecutive failures are zero. Otherwise it reports `event: "stalled"` and includes the same consumer diagnostics. A running launchd or systemd process alone is not delivery proof.

Compaction stops at the oldest cursor and names every consumer that pins that boundary. If no bytes can be removed, it returns `compaction_blocked` and tells you to inspect or forget the named consumer. It also removes terminal timer and recurring-schedule records whose terminal events are safely inside the consumed prefix, plus stopped stopwatch records when events are removed. A journal repairs cursor offsets if compaction is interrupted after replacing the log.

## Development

Run the test suite:

```sh
python3 -m unittest discover -s tests -v
```

Build the wheel and source archive with [uv](https://docs.astral.sh/uv/):

```sh
uv build
```

The GitHub workflow uses two jobs: Python 3.10 on Linux and Python 3.14 on macOS.

## License

MIT. See [LICENSE](LICENSE).
