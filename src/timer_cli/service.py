from __future__ import annotations

import json
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from .delivery import delivery_health, seed_consumer_at_tail

SERVICE_LABEL = "com.tszaks.timer-supervisor"
DEFAULT_DELIVERY_LEASE = 60.0


class ServiceError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def checked_binary(env_name: str, default: str) -> str:
    configured = os.environ.get(env_name)
    binary = configured or shutil.which(default)
    if not binary:
        raise ServiceError("missing_dependency", f"required command not found: {default}")
    candidate = Path(binary).expanduser().absolute()
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        raise ServiceError("missing_dependency", f"required command is not executable: {candidate}")
    return str(candidate)


def run_checked(arguments: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            arguments,
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise ServiceError("missing_dependency", f"could not run {arguments[0]}: {error}") from error
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise ServiceError("service_failed", f'{Path(arguments[0]).name} failed: {detail}')
    return result


def synthetic_event(namespace: str) -> dict[str, Any]:
    return {
        "schema": "timer.event.v1",
        "event_id": "setup-dry-run",
        "event": "expired",
        "id": "setup-dry-run",
        "key": "setup-dry-run",
        "timestamp": "1970-01-01T00:00:00+00:00",
        "namespace": namespace,
        "owner": "timer-setup",
        "ref": "thread:setup-dry-run",
        "message": "Timer service setup dry run.",
    }


def service_environment_path(*binaries: str) -> str:
    directories = [str(Path(binary).parent) for binary in binaries]
    directories.extend(os.environ.get("PATH", os.defpath).split(os.pathsep))
    return os.pathsep.join(dict.fromkeys(directory for directory in directories if directory))


def preflight(namespace: str) -> dict[str, str]:
    codex_bin = checked_binary("TIMER_CODEX_BIN", "codex")
    supervisor_bin = checked_binary("TIMER_SUPERVISOR_BIN", "timer-supervisor")
    queue_help = run_checked([codex_bin, "queue", "--help"]).stdout
    if "--thread" not in queue_help or "session" not in queue_help.casefold():
        raise ServiceError(
            "preflight_failed",
            "codex queue must expose --thread for session UUIDs or exact session names",
        )
    result = run_checked(
        [supervisor_bin, "--dry-run", "--codex-bin", codex_bin],
        input_text=json.dumps(synthetic_event(namespace)),
    )
    try:
        envelope = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise ServiceError("preflight_failed", "timer-supervisor dry run returned invalid JSON") from error
    if envelope.get("schema") != "timer.next-turn.v1":
        raise ServiceError("preflight_failed", "timer-supervisor dry run returned the wrong schema")
    return {
        "codex_bin": codex_bin,
        "supervisor_bin": supervisor_bin,
        "service_path": service_environment_path(codex_bin, supervisor_bin),
    }


def platform_name() -> str:
    override = os.environ.get("TIMER_SERVICE_PLATFORM")
    if override:
        return override
    if sys.platform == "darwin":
        return "launchd"
    if sys.platform.startswith("linux"):
        return "systemd"
    raise ServiceError("unsupported_platform", "service setup supports macOS launchd and Linux systemd")


def service_path(platform: str) -> Path:
    override = os.environ.get("TIMER_SERVICE_PATH")
    if override:
        return Path(override).expanduser()
    if platform == "launchd":
        return Path.home() / "Library" / "LaunchAgents" / f"{SERVICE_LABEL}.plist"
    return Path.home() / ".config" / "systemd" / "user" / "timer-supervisor.service"


def render_launchd(
    timer_bin: str,
    supervisor_bin: str,
    codex_bin: str,
    environment_path: str,
    namespace: str,
    log_dir: Path,
) -> bytes:
    payload = {
        "Label": SERVICE_LABEL,
        "ProgramArguments": [
            timer_bin,
            "daemon",
            "--hook",
            supervisor_bin,
            "--namespace",
            namespace,
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ThrottleInterval": 2,
        "EnvironmentVariables": {
            "TIMER_CODEX_BIN": codex_bin,
            "PATH": environment_path,
        },
        "StandardOutPath": str(log_dir / "timer-supervisor.log"),
        "StandardErrorPath": str(log_dir / "timer-supervisor.error.log"),
    }
    return plistlib.dumps(payload, sort_keys=False)


def systemd_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render_systemd(
    timer_bin: str,
    supervisor_bin: str,
    codex_bin: str,
    environment_path: str,
    namespace: str,
) -> bytes:
    content = f"""[Unit]
Description=Timer continuation supervisor
After=default.target

[Service]
Type=simple
Environment={systemd_quote(f'TIMER_CODEX_BIN={codex_bin}')}
Environment={systemd_quote(f'PATH={environment_path}')}
ExecStart={systemd_quote(timer_bin)} daemon --hook {systemd_quote(supervisor_bin)} --namespace {systemd_quote(namespace)}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""
    return content.encode()


def install_service(state_file: Path, *, timer_bin: str, namespace: str, dry_run: bool) -> dict[str, Any]:
    binaries = preflight(namespace)
    platform = platform_name()
    target = service_path(platform)
    log_dir = Path.home() / "Library" / "Logs" / "Timer" if platform == "launchd" else Path.home() / ".local" / "state" / "timer"
    unit = (
        render_launchd(
            timer_bin,
            binaries["supervisor_bin"],
            binaries["codex_bin"],
            binaries["service_path"],
            namespace,
            log_dir,
        )
        if platform == "launchd"
        else render_systemd(
            timer_bin,
            binaries["supervisor_bin"],
            binaries["codex_bin"],
            binaries["service_path"],
            namespace,
        )
    )
    result: dict[str, Any] = {
        "ok": True,
        "schema": "timer.service.v1",
        "event": "validated" if dry_run else "installed",
        "platform": platform,
        "path": str(target),
        "checks": ["codex_queue", "synthetic_expiry"],
    }
    if dry_run:
        return result

    target.parent.mkdir(parents=True, exist_ok=True)
    log_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(unit)
    temporary.replace(target)

    consumer = f'daemon-hook:{Path(binaries["supervisor_bin"]).resolve()!s}'
    seeded = seed_consumer_at_tail(
        state_file,
        consumer=consumer,
        namespace=namespace,
        event_type="expired,tick",
    )
    result["consumer"] = consumer
    result["consumer_created"] = seeded["created"]

    if platform == "launchd":
        launchctl = checked_binary("TIMER_LAUNCHCTL_BIN", "launchctl")
        domain = f"gui/{os.getuid()}"
        subprocess.run(
            [launchctl, "bootout", domain, str(target)],
            text=True,
            capture_output=True,
            check=False,
        )
        run_checked([launchctl, "bootstrap", domain, str(target)])
        run_checked([launchctl, "kickstart", "-k", f"{domain}/{SERVICE_LABEL}"])
        run_checked([launchctl, "print", f"{domain}/{SERVICE_LABEL}"])
    else:
        systemctl = checked_binary("TIMER_SYSTEMCTL_BIN", "systemctl")
        run_checked([systemctl, "--user", "daemon-reload"])
        run_checked([systemctl, "--user", "enable", "--now", target.name])
        run_checked([systemctl, "--user", "is-active", target.name])
    result["checks"].append("service_running")
    return result


def service_hook_path(target: Path, platform: str) -> str | None:
    if not target.exists():
        return None
    try:
        if platform == "launchd":
            arguments = plistlib.loads(target.read_bytes()).get("ProgramArguments", [])
        else:
            line = next(
                line for line in target.read_text(encoding="utf-8").splitlines()
                if line.startswith("ExecStart=")
            )
            arguments = shlex.split(line.removeprefix("ExecStart="))
        index = arguments.index("--hook")
        return str(Path(arguments[index + 1]).expanduser().resolve())
    except (IndexError, StopIteration, ValueError, OSError, plistlib.InvalidFileException):
        return None


def service_status(
    state_file: Path,
    *,
    delivery_lease: float = DEFAULT_DELIVERY_LEASE,
) -> dict[str, Any]:
    platform = platform_name()
    target = service_path(platform)
    loaded = False
    if platform == "launchd":
        command = checked_binary("TIMER_LAUNCHCTL_BIN", "launchctl")
        result = subprocess.run(
            [command, "print", f"gui/{os.getuid()}/{SERVICE_LABEL}"],
            text=True,
            capture_output=True,
            check=False,
        )
        loaded = result.returncode == 0
    else:
        command = checked_binary("TIMER_SYSTEMCTL_BIN", "systemctl")
        result = subprocess.run(
            [command, "--user", "is-active", target.name],
            text=True,
            capture_output=True,
            check=False,
        )
        loaded = result.returncode == 0 and result.stdout.strip() == "active"
    hook = service_hook_path(target, platform)
    consumer = f"daemon-hook:{hook}" if hook else "daemon-hook:unknown"
    health = delivery_health(
        state_file,
        consumer=consumer,
        running=loaded,
        delivery_lease=delivery_lease,
    )
    delivering = bool(target.exists() and health["delivering"])
    return {
        "ok": delivering,
        "schema": "timer.service.v1",
        "event": "delivering" if delivering else "stalled",
        "platform": platform,
        "path": str(target),
        "installed": target.exists(),
        "running": loaded,
        "consumer": health,
    }


def uninstall_service() -> dict[str, Any]:
    platform = platform_name()
    target = service_path(platform)
    if platform == "launchd":
        command = checked_binary("TIMER_LAUNCHCTL_BIN", "launchctl")
        if target.exists():
            subprocess.run(
                [command, "bootout", f"gui/{os.getuid()}", str(target)],
                text=True,
                capture_output=True,
                check=False,
            )
    else:
        command = checked_binary("TIMER_SYSTEMCTL_BIN", "systemctl")
        if target.exists():
            subprocess.run(
                [command, "--user", "disable", "--now", target.name],
                text=True,
                capture_output=True,
                check=False,
            )
    existed = target.exists()
    target.unlink(missing_ok=True)
    return {
        "ok": True,
        "schema": "timer.service.v1",
        "event": "uninstalled" if existed else "not_installed",
        "platform": platform,
        "path": str(target),
    }
