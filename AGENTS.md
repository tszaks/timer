# Timer

A persistent timer, stopwatch and deferred-work CLI for Unix systems. Python 3.10+, standard library only.

This file is for AI agents that work **on** Timer's code. If you are an agent that **uses** Timer to schedule later work, read [skills/timer-agent/SKILL.md](skills/timer-agent/SKILL.md) instead.

## Build, test
- Run the CLI from the checkout: `./timer ...`. Nothing to install.
- Test: `python3 -m unittest discover -s tests -v`. Run the **whole** suite before you say something works. It takes about 20 to 30 seconds.
- Build the wheel and source archive: `uv build`.
- Always set `TIMER_STATE`, `TIMER_EVENTS` and `TIMER_CONSUMERS` to a scratch folder when you try commands by hand. Otherwise you write into the real `~/.local/share/timer`.

## Code map
- `src/timer_cli/cli.py`: argument parsing, timers, stopwatches, event log, every command.
- `src/timer_cli/delivery.py`: consumers, leases, claim, ack, nack, compaction, daemon delivery.
- `src/timer_cli/scheduler.py`: recurring schedules.
- `src/timer_cli/service.py`: `timer setup` and `timer service` (launchd and systemd user services).
- `src/timer_cli/supervisor.py`: `timer-supervisor`, which turns an event into a `codex queue` call.
- `tests/test_cli.py`: all tests.
- `examples/`: reference launchd and systemd units. `skills/timer-agent/`: the agent skill.

## Conventions
- Match the surrounding code: naming, comment density, idiom. Comments say **why**, not what.
- No third-party runtime dependencies.
- Delivery is at least once. Changes to claim, ack, nack, leases, cursors or compaction need a test that **fails without the change**. Do not weaken a delivery test to make a change pass.
- With `--json`, expected domain errors return structured JSON and exit 0; human mode exits nonzero. Keep that contract and `timer schema --json` in step.
- Do not run `timer setup` or `timer service` against a real machine unless asked. They install a user service.
- Update `README.md` and `CHANGELOG.md` when you change a command, flag, environment variable or the JSON schema.
