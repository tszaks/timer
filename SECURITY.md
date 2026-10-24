# Security policy

## Supported versions

Only the latest version of Timer gets security fixes.

| Version | Supported |
|---|---|
| 0.8.x (latest) | Yes |
| Older | No |

## Reporting a vulnerability

Please report security problems privately, not in a public issue.

Use GitHub private vulnerability reporting: open the **Security** tab of this repository and choose **Report a vulnerability**. If that is not available to you, email tyler@szakacsmedia.com.

Please include:

- What you found and what an attacker could do with it.
- Steps to reproduce.
- Your OS, Python version and Timer version (`pyproject.toml`).

You can expect a first reply within a few days. Timer is maintained by one person, so a fix may take longer, but you will get updates. Once a fix is released you will be credited in the changelog unless you prefer to stay anonymous.

## What counts

Timer keeps state in plain files under your user account and can run programs you name. These areas matter most:

- **Hooks.** `timer daemon --hook` runs an executable you choose, with event data on standard input. Anything that makes Timer run a program the user did not choose is a vulnerability.
- **The wake service.** `timer setup` writes and starts a launchd or systemd user service. It should never write outside your user account's service folders.
- **The Codex supervisor.** `timer-supervisor` queues new Codex turns from event payloads. Payload text becomes agent input, so treat any process that can write to the event log as able to instruct your agent.
- **State files.** `timers.json`, `events.jsonl` and `consumers.json` are not encrypted and not access controlled beyond file permissions. Do not put secrets in `--message` or `--payload`.
- **Data loss.** A bug that drops or repeats a delivery silently is treated as high priority even when it is not exploitable.

Timer makes no network requests of its own.
