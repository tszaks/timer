# Contributing to Timer

Thanks for helping. Timer is a small CLI with a narrow job: keep time on disk and hand expirations to people or agents without losing or repeating work.

By taking part you agree to follow the [Code of Conduct](CODE_OF_CONDUCT.md).

## Before you start

- For bugs, open an issue with the template. The exact command and its output are the most useful things to attach.
- For features, open an issue first and describe the problem you want to solve. Timer has no runtime dependencies and stays that way.
- Small fixes (typos, clear bugs) can go straight to a pull request.
- Security problems go through [SECURITY.md](SECURITY.md), not a public issue.

## Setup

You need Python 3.10 or newer. There is nothing to install for development.

```sh
git clone https://github.com/tszaks/timer.git
cd timer
./timer 10m test    # run the CLI straight from the checkout
```

To use an isolated state while you try things, point Timer at a scratch folder:

```sh
export TIMER_STATE=/tmp/timer-dev/timers.json
export TIMER_EVENTS=/tmp/timer-dev/events.jsonl
export TIMER_CONSUMERS=/tmp/timer-dev/consumers.json
```

## Run the tests

```sh
python3 -m unittest discover -s tests -v
```

Run the whole suite before you open a pull request. CI runs it on Python 3.10 (Linux) and Python 3.14 (macOS).

## Code style

- **Match the surrounding code.** Same naming, same comment density, same idiom as the file you are editing. There is no formatter to run, so read the neighbors.
- Standard library only. No new runtime dependencies without discussion.
- Comments explain *why*, not *what*.

## Delivery safety

Timer promises at-least-once delivery with leases. A change to claiming, acknowledging, leases, cursors or compaction needs a test that fails without the change. Do not weaken an existing delivery test to make a change pass. If a test is wrong, say why in the pull request.

## Pull request checklist

- [ ] The full test suite passes locally.
- [ ] New behavior has a test.
- [ ] The change is small and focused. Unrelated cleanups go in their own pull request.
- [ ] The code matches the surrounding style and adds no dependency.
- [ ] README updated if a command, flag, environment variable or the JSON schema changed.
- [ ] A line added under `[Unreleased]` in [CHANGELOG.md](CHANGELOG.md) for anything a user would notice.

## Using AI agents to contribute

Fine, as long as you read and understand what you submit and the checklist above is true. [AGENTS.md](AGENTS.md) tells coding agents how this repository works.

## License

By contributing you agree that your contribution is licensed under the [MIT License](LICENSE).
