# Changelog

All notable changes to Timer are listed here. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/). Timer has no tagged releases. Versions below are the `version` in `pyproject.toml` at the matching commit, and dates come from the commit history.

## [Unreleased]

## [0.8.0] - 2026-09-16

### Added
- Delivery health is observable: `timer consumers` and `timer service status` report lag, lease age, failures and last progress. A running service process alone is no longer treated as proof of delivery.

## [0.7.0] - 2026-09-16

### Added
- Recurring schedules appear in `timer`, `timer list` and `timer status`.
- A backlog limit (`--max-unacked`) pauses a recurring series when ticks go unacknowledged.

## [0.6.0] - 2026-09-15

### Changed
- Hardened wake delivery.

### Fixed
- The installed wake service keeps the runtime `PATH`.

## [0.5.0] - 2026-09-15

### Added
- Delivery protocol release: leased `claim`, `ack` and `nack`, consumers, `compact` and `forget`, `timer schema --json`, and the Codex supervisor.

## [0.4.0] - 2026-09-15

### Added
- Durable agent scheduler primitives: keyed timers, namespaces and owners, event log, daemon.

## [0.3.0] - 2026-09-15

### Added
- First public version (published as Agent Timer CLI, renamed to Timer): persistent timers and stopwatches on disk.
