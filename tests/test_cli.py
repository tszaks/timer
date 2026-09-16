from __future__ import annotations

import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLI = ROOT / "timer"


class TimerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.env = dict(os.environ)
        for name in (
            "TIMER_CONSUMERS", "TIMER_EVENTS", "TIMER_NAMESPACE", "TIMER_OWNER",
            "TIMER_CODEX_BIN", "TIMER_SUPERVISOR_BIN", "TIMER_SERVICE_PLATFORM",
            "TIMER_SERVICE_PATH", "TIMER_LAUNCHCTL_BIN", "TIMER_SYSTEMCTL_BIN",
        ):
            self.env.pop(name, None)
        self.env["TIMER_STATE"] = str(Path(self.temp.name) / "timers.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(CLI), *args], text=True, capture_output=True, env=self.env, timeout=3,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def run_supervisor(
        self, event: dict[str, object], *args: str, expected: int = 0
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env["PYTHONPATH"] = str(ROOT / "src")
        result = subprocess.run(
            [sys.executable, "-m", "timer_cli.supervisor", *args],
            input=json.dumps(event),
            text=True,
            capture_output=True,
            env=env,
            timeout=3,
            check=False,
        )
        self.assertEqual(result.returncode, expected, result.stderr)
        return result

    def test_start_status_and_cancel_by_label(self) -> None:
        started = json.loads(self.run_cli("start", "10m", "--label", "rice", "--json").stdout)
        self.assertEqual(started["label"], "rice")
        self.assertEqual(started["status"], "active")
        self.assertGreater(started["remaining_seconds"], 599)

        status = json.loads(self.run_cli("status", "rice", "--json").stdout)
        self.assertEqual(status["id"], started["id"])

        cancelled = json.loads(self.run_cli("cancel", "rice", "--json").stdout)
        self.assertEqual(cancelled["status"], "cancelled")
        waited = json.loads(self.run_cli("wait", "rice", "--json").stdout)
        self.assertEqual(waited["event"], "cancelled")

    def test_state_persists_across_processes_and_id_prefix_works(self) -> None:
        started = json.loads(self.run_cli("start", "10m", "--label", "persistent", "--json").stdout)

        status = json.loads(self.run_cli("status", started["id"][:8], "--json").stdout)

        self.assertEqual(status["id"], started["id"])
        self.assertTrue(Path(self.env["TIMER_STATE"]).is_file())

    def test_wait_returns_expiration_event(self) -> None:
        started = json.loads(self.run_cli("start", "0.1s", "--label", "test", "--json").stdout)
        waited = json.loads(self.run_cli("wait", started["id"], "--json").stdout)
        self.assertEqual(waited["status"], "expired")
        self.assertEqual(waited["remaining_seconds"], 0)

    def test_list_hides_expired_by_default(self) -> None:
        self.run_cli("start", "0.05s", "--label", "short")
        self.run_cli("wait", "short")
        active = json.loads(self.run_cli("list", "--json").stdout)
        all_timers = json.loads(self.run_cli("list", "--all", "--json").stdout)
        self.assertEqual(active, [])
        self.assertEqual(len(all_timers), 1)

    def test_wait_tracks_original_timer_when_duplicate_start_is_rejected(self) -> None:
        first = json.loads(self.run_cli("start", "1.5s", "--label", "same", "--json").stdout)
        waiter = subprocess.Popen(
            [str(CLI), "wait", "same", "--poll-interval", "0.02", "--json"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        time.sleep(0.05)
        duplicate = self.run_cli("start", "10m", "--label", "same", "--json")
        stdout, stderr = waiter.communicate(timeout=5)

        duplicate_payload = json.loads(duplicate.stdout)
        self.assertFalse(duplicate_payload["ok"])
        self.assertEqual(duplicate_payload["error"]["code"], "conflict")
        self.assertEqual(waiter.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["id"], first["id"])

    def test_ambiguous_id_prefix_and_invalid_poll_interval_are_rejected(self) -> None:
        state = {
            "timers": [
                {"id": "abc111", "label": "one", "status": "active", "created_at": 1, "due_at": 9999999999},
                {"id": "abc222", "label": "two", "status": "active", "created_at": 2, "due_at": 9999999999},
            ]
        }
        Path(self.env["TIMER_STATE"]).write_text(json.dumps(state), encoding="utf-8")

        ambiguous = self.run_cli("status", "abc", expected=2)
        invalid_poll = self.run_cli("wait", "abc111", "--poll-interval", "0", expected=2)

        self.assertIn("ambiguous", ambiguous.stderr)
        self.assertIn("greater than zero", invalid_poll.stderr)

    def test_multiple_timers_are_independent(self) -> None:
        first = json.loads(self.run_cli("start", "0.5s", "--label", "first", "--json").stdout)
        second = json.loads(self.run_cli("start", "10m", "--label", "second", "--json").stdout)

        active = json.loads(self.run_cli("list", "--json").stdout)
        self.assertEqual({item["id"] for item in active}, {first["id"], second["id"]})
        self.assertEqual(json.loads(self.run_cli("status", "first", "--json").stdout)["id"], first["id"])
        self.assertEqual(json.loads(self.run_cli("wait", "first", "--json").stdout)["status"], "expired")
        self.assertEqual(json.loads(self.run_cli("status", "second", "--json").stdout)["status"], "active")
        self.assertEqual(json.loads(self.run_cli("cancel", "second", "--json").stdout)["status"], "cancelled")

    def test_stopwatch_pause_resume_lap_reset_and_stop(self) -> None:
        started = json.loads(
            self.run_cli("stopwatch", "start", "--label", "prep", "--json").stdout
        )
        time.sleep(0.04)
        first_lap = json.loads(self.run_cli("stopwatch", "lap", "prep", "--json").stdout)
        self.assertEqual(first_lap["laps"][0]["number"], 1)
        self.assertGreater(first_lap["laps"][0]["elapsed_seconds"], 0)

        paused = json.loads(self.run_cli("stopwatch", "pause", "prep", "--json").stdout)
        paused_elapsed = paused["elapsed_seconds"]
        time.sleep(0.04)
        status = json.loads(self.run_cli("stopwatch", "status", "prep", "--json").stdout)
        self.assertEqual(status["elapsed_seconds"], paused_elapsed)

        resumed = json.loads(self.run_cli("stopwatch", "resume", "prep", "--json").stdout)
        self.assertEqual(resumed["status"], "running")
        time.sleep(0.04)
        reset = json.loads(self.run_cli("stopwatch", "reset", "prep", "--json").stdout)
        self.assertEqual(reset["laps"], [])
        self.assertLess(reset["elapsed_seconds"], 0.02)

        stopped = json.loads(self.run_cli("stopwatch", "stop", started["id"], "--json").stdout)
        self.assertEqual(stopped["status"], "stopped")
        self.assertGreaterEqual(stopped["elapsed_seconds"], 0)
        self.assertEqual(json.loads(self.run_cli("stopwatch", "list", "--json").stdout), [])
        all_stopwatches = json.loads(self.run_cli("stopwatch", "list", "--all", "--json").stdout)
        self.assertEqual(all_stopwatches[0]["id"], started["id"])

    def test_stopwatch_persists_across_processes(self) -> None:
        started = json.loads(
            self.run_cli("stopwatch", "start", "--label", "persistent-watch", "--json").stdout
        )
        status = json.loads(
            self.run_cli("stopwatch", "status", started["id"][:8], "--json").stdout
        )
        self.assertEqual(status["id"], started["id"])
        self.assertEqual(status["status"], "running")

    def test_terse_timer_interface(self) -> None:
        labeled = json.loads(self.run_cli("10m", "rice", "--json").stdout)
        generated = json.loads(self.run_cli("30s", "--json").stdout)

        self.assertEqual(labeled["label"], "rice")
        self.assertEqual(generated["label"], "30s timer")
        self.assertEqual(json.loads(self.run_cli("rice", "--json").stdout)["id"], labeled["id"])
        self.assertEqual(
            json.loads(self.run_cli("cancel", "rice", "--json").stdout)["status"],
            "cancelled",
        )

    def test_terse_stopwatch_interface(self) -> None:
        started = json.loads(
            self.run_cli("stopwatch", "start", "cooking", "--json").stdout
        )
        status = json.loads(self.run_cli("stopwatch", "cooking", "--json").stdout)

        self.assertEqual(started["label"], "cooking")
        self.assertEqual(status["id"], started["id"])

    def test_human_output_uses_compact_whole_time(self) -> None:
        timer_output = self.run_cli("25m31s", "peppers").stdout
        stopwatch_output = self.run_cli("stopwatch", "start", "prep").stdout
        json_output = json.loads(self.run_cli("peppers", "--json").stdout)

        self.assertIn("25m 31s remaining", timer_output)
        self.assertNotIn(".", timer_output.split(" remaining")[0])
        self.assertNotIn("[", timer_output)
        self.assertIn("0s elapsed", stopwatch_output)
        self.assertNotIn("[", stopwatch_output)
        self.assertIsInstance(json_output["remaining_seconds"], (int, float))
        self.assertIn("id", json_output)

    def test_adaptive_units_preserve_original_largest_unit(self) -> None:
        seconds = self.run_cli("30s", "seconds").stdout
        minutes = self.run_cli("15m", "minutes").stdout
        hours = self.run_cli("1h5m", "hours").stdout

        self.assertIn("30s remaining", seconds)
        self.assertIn("15m 00s remaining", minutes)
        self.assertIn("1h 05m 00s remaining", hours)

        state_path = Path(self.env["TIMER_STATE"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        minute_timer = next(item for item in state["timers"] if item["label"] == "minutes")
        minute_timer["due_at"] = time.time() + 58.2
        state_path.write_text(json.dumps(state), encoding="utf-8")
        transition = self.run_cli("minutes").stdout
        self.assertIn("0m 59s remaining", transition)

    def test_watch_is_read_only_and_handles_interrupt(self) -> None:
        self.run_cli("15m", "watch-me")
        state_path = Path(self.env["TIMER_STATE"])
        before = state_path.read_bytes()
        before_mtime = state_path.stat().st_mtime_ns
        snapshot = self.run_cli("watch", "--once").stdout

        self.assertIn("watch-me: 15m 00s remaining", snapshot)
        self.assertNotIn("[", snapshot)
        self.assertEqual(state_path.read_bytes(), before)
        self.assertEqual(state_path.stat().st_mtime_ns, before_mtime)

        watcher = subprocess.Popen(
            [str(CLI), "watch"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=self.env
        )
        time.sleep(0.1)
        watcher.send_signal(signal.SIGINT)
        _, stderr = watcher.communicate(timeout=2)
        self.assertEqual(watcher.returncode, 0, stderr)

    def test_bare_timer_and_status_defaults(self) -> None:
        self.assertIn("timer 10m rice", self.run_cli().stdout)
        self.assertIn("timer 10m rice", self.run_cli("status").stdout)
        self.run_cli("10m", "one")
        self.assertIn("one:", self.run_cli().stdout)
        self.assertIn("one:", self.run_cli("status").stdout)
        self.run_cli("20m", "two")
        status = self.run_cli("status").stdout
        self.assertIn("one:", status)
        self.assertIn("two:", status)

    def test_cancel_without_identifier_is_safe(self) -> None:
        self.assertIn("no active timer", self.run_cli("cancel", expected=2).stderr)
        self.run_cli("10m", "only")
        self.assertIn("cancelled", self.run_cli("cancel").stdout)
        self.run_cli("10m", "one")
        self.run_cli("10m", "two")
        ambiguous = self.run_cli("cancel", expected=2)
        self.assertNotIn("usage:", ambiguous.stderr)
        self.assertIn("one", ambiguous.stderr)
        self.assertIn("two", ambiguous.stderr)

    def test_stopwatch_defaults_and_shortcuts(self) -> None:
        self.assertIn("stopwatch start prep", self.run_cli("stopwatch").stdout)
        self.run_cli("stopwatch", "start", "prep")
        self.assertIn("prep:", self.run_cli("stopwatch").stdout)
        self.assertIn("prep:", self.run_cli("lap").stdout)
        self.assertIn("paused", self.run_cli("pause").stdout)
        self.assertIn("running", self.run_cli("resume").stdout)
        self.assertIn("prep:", self.run_cli("reset").stdout)

        self.run_cli("stopwatch", "start", "second")
        ambiguous = self.run_cli("lap", expected=2)
        self.assertNotIn("usage:", ambiguous.stderr)
        self.assertIn("prep", ambiguous.stderr)
        self.assertIn("second", ambiguous.stderr)

    def test_active_labels_are_unique_and_reusable(self) -> None:
        first = json.loads(self.run_cli("10m", "Peppers", "--json").stdout)
        duplicate = self.run_cli("20m", "peppers", expected=2)
        self.assertIn("peppers-2", duplicate.stderr)

        self.run_cli("cancel", first["id"])
        reused = json.loads(self.run_cli("20m", "peppers", "--json").stdout)
        self.assertEqual(reused["label"], "peppers")

        self.run_cli("0.05s", "short")
        self.run_cli("wait", "short")
        self.assertEqual(json.loads(self.run_cli("1m", "SHORT", "--json").stdout)["label"], "SHORT")

    def test_concurrent_duplicate_starts_allow_only_one(self) -> None:
        processes = [
            subprocess.Popen(
                [str(CLI), "10m", "shared", "--json"],
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self.env,
            )
            for _ in range(4)
        ]
        results = [process.communicate(timeout=3) + (process.returncode,) for process in processes]
        payloads = [json.loads(stdout) for stdout, _, _ in results]
        self.assertEqual(sum(payload["ok"] for payload in payloads), 1)
        self.assertEqual(sum(not payload["ok"] for payload in payloads), 3)
        self.assertTrue(all(returncode == 0 for _, _, returncode in results))

    def test_rename_preserves_deadline_and_enforces_uniqueness(self) -> None:
        first = json.loads(self.run_cli("10m", "first", "--json").stdout)
        self.run_cli("20m", "second")
        before = json.loads(Path(self.env["TIMER_STATE"]).read_text(encoding="utf-8"))
        before_due = next(item["due_at"] for item in before["timers"] if item["id"] == first["id"])

        renamed = json.loads(self.run_cli("rename", "first", "renamed", "--json").stdout)
        after = json.loads(Path(self.env["TIMER_STATE"]).read_text(encoding="utf-8"))
        after_due = next(item["due_at"] for item in after["timers"] if item["id"] == first["id"])
        self.assertEqual(renamed["label"], "renamed")
        self.assertEqual(before_due, after_due)

        conflict = self.run_cli("rename", "renamed", "SECOND", expected=2)
        self.assertIn("second-2", conflict.stderr.lower())

    def test_idempotent_key_preserves_continuation_payload(self) -> None:
        first = json.loads(
            self.run_cli(
                "start", "10m", "--key", "rice", "--message", "Check the pot",
                "--ref", "task:cook-1", "--json",
            ).stdout
        )
        retried = json.loads(self.run_cli("start", "10m", "--key", "rice", "--json").stdout)
        conflict = json.loads(self.run_cli("start", "11m", "--key", "rice", "--json").stdout)

        self.assertEqual(first["id"], retried["id"])
        self.assertEqual(retried["event"], "existing")
        self.assertEqual(retried["message"], "Check the pot")
        self.assertEqual(retried["ref"], "task:cook-1")
        self.assertFalse(conflict["ok"])
        self.assertEqual(conflict["error"]["code"], "conflict")

    def test_pending_peeks_and_drain_acknowledges_events(self) -> None:
        self.run_cli(
            "start", "0.05s", "--key", "rice", "--message", "Check the pot",
            "--ref", "task:cook-1", "--json",
        )
        self.run_cli("wait", "--key", "rice", "--json")

        first_peek = json.loads(self.run_cli("pending", "--event", "expired", "--json").stdout)
        second_peek = json.loads(self.run_cli("pending", "--event", "expired", "--json").stdout)
        drained = json.loads(self.run_cli("drain", "--event", "expired", "--json").stdout)
        empty = json.loads(self.run_cli("pending", "--event", "expired", "--json").stdout)

        self.assertEqual(first_peek, second_peek)
        self.assertEqual(first_peek, drained)
        self.assertEqual(first_peek[0]["schema"], "timer.event.v1")
        self.assertEqual(first_peek[0]["message"], "Check the pot")
        self.assertEqual(first_peek[0]["ref"], "task:cook-1")
        self.assertEqual(empty, [])

    def test_daemon_writes_wake_file_for_expired_timer(self) -> None:
        wake_dir = Path(self.temp.name) / "wake"
        self.run_cli("start", "0.05s", "--key", "oven", "--message", "Check dinner")
        time.sleep(0.08)
        self.run_cli("daemon", "--once", "--wake-dir", str(wake_dir))

        files = list(wake_dir.glob("*.json"))
        self.assertEqual(len(files), 1)
        event = json.loads(files[0].read_text(encoding="utf-8"))
        self.assertEqual(event["event"], "expired")
        self.assertEqual(event["key"], "oven")
        self.assertEqual(event["message"], "Check dinner")

    def test_daemon_hook_receives_event_on_standard_input(self) -> None:
        hook = Path(self.temp.name) / "hook.sh"
        output = Path(self.temp.name) / "hook-event.json"
        hook.write_text('#!/bin/sh\ncat > "$TIMER_HOOK_OUTPUT"\n', encoding="utf-8")
        hook.chmod(0o700)
        self.env["TIMER_HOOK_OUTPUT"] = str(output)
        self.run_cli("start", "0.05s", "--key", "hooked", "--message", "Continue work")
        time.sleep(0.08)

        self.run_cli("daemon", "--once", "--hook", str(hook))

        event = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(event["schema"], "timer.event.v1")
        self.assertEqual(event["key"], "hooked")
        self.assertEqual(event["message"], "Continue work")

    def test_daemon_hook_and_wake_directory_have_independent_delivery(self) -> None:
        hook = Path(self.temp.name) / "hook.sh"
        hook_output = Path(self.temp.name) / "hook-event.json"
        wake_dir = Path(self.temp.name) / "wake"
        hook.write_text('#!/bin/sh\ncat > "$TIMER_HOOK_OUTPUT"\n', encoding="utf-8")
        hook.chmod(0o700)
        self.env["TIMER_HOOK_OUTPUT"] = str(hook_output)
        self.run_cli("start", "0.05s", "--key", "both-paths", "--message", "Resume work")
        time.sleep(0.08)

        self.run_cli(
            "daemon", "--once", "--hook", str(hook), "--wake-dir", str(wake_dir)
        )

        hook_event = json.loads(hook_output.read_text(encoding="utf-8"))
        wake_files = list(wake_dir.glob("*.json"))
        self.assertEqual(len(wake_files), 1)
        wake_event = json.loads(wake_files[0].read_text(encoding="utf-8"))
        self.assertEqual(hook_event["event_id"], wake_event["event_id"])

    def test_failed_daemon_hook_records_error_and_daemon_stays_running(self) -> None:
        hook = Path(self.temp.name) / "retry.sh"
        hook.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        hook.chmod(0o700)
        self.run_cli("start", "0.05s", "--key", "retry-hook")
        time.sleep(0.08)

        daemon = subprocess.Popen(
            [
                str(CLI), "daemon", "--hook", str(hook), "--poll-interval", "1",
                "--max-hook-failures", "5",
            ],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        try:
            time.sleep(0.2)
            self.assertIsNone(daemon.poll())
            consumers = json.loads(self.run_cli("consumers", "--json").stdout)["consumers"]
            record = next(item for item in consumers if item["name"].startswith("daemon-hook:"))
            human = self.run_cli("consumers").stdout
            claimed = json.loads(
                self.run_cli(
                    "claim", "--consumer", "observer", "--event", "expired", "--json"
                ).stdout
            )
            self.assertEqual(record["consecutive_failures"], 1)
            self.assertEqual(record["last_error"]["code"], "hook_failed")
            self.assertIn("age_seconds", record["last_error"])
            self.assertIn("fails=1", human)
            self.assertIn("last_error: hook_failed", human)
            self.assertEqual(claimed["event"], "claimed")
        finally:
            daemon.send_signal(signal.SIGINT)
            daemon.communicate(timeout=2)

    def test_poison_hook_event_is_skipped_only_for_that_consumer(self) -> None:
        hook = Path(self.temp.name) / "poison.sh"
        output = Path(self.temp.name) / "delivered.json"
        hook.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        hook.chmod(0o700)
        self.run_cli("start", "0.05s", "--key", "poison")
        self.run_cli("start", "0.05s", "--key", "healthy")
        time.sleep(0.08)

        attempts = [
            self.run_cli(
                "daemon", "--once", "--hook", str(hook), "--max-hook-failures", "5"
            )
            for _ in range(5)
        ]
        self.assertIn("dropped", attempts[-1].stderr)
        hook.write_text('#!/bin/sh\ncat > "$TIMER_HOOK_OUTPUT"\n', encoding="utf-8")
        self.env["TIMER_HOOK_OUTPUT"] = str(output)
        self.run_cli("daemon", "--once", "--hook", str(hook))

        delivered = json.loads(output.read_text(encoding="utf-8"))
        other_consumer = json.loads(
            self.run_cli(
                "claim", "--consumer", "independent", "--event", "expired", "--json"
            ).stdout
        )
        hook_record = next(
            item for item in json.loads(self.run_cli("consumers", "--json").stdout)["consumers"]
            if item["name"].startswith("daemon-hook:")
        )
        self.assertEqual(delivered["key"], "healthy")
        self.assertEqual(other_consumer["delivery"]["key"], "poison")
        self.assertEqual(hook_record["skipped_events"], 1)

    def test_json_domain_errors_exit_success_with_stable_schema(self) -> None:
        missing = self.run_cli("status", "--key", "missing", "--json")
        payload = json.loads(missing.stdout)

        self.assertEqual(missing.returncode, 0)
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["schema"], "timer.v1")
        self.assertEqual(payload["error"]["code"], "not_found")
        schema = json.loads(self.run_cli("schema", "--json").stdout)
        self.assertEqual(schema["contracts"]["event"]["schema"], "timer.event.v1")

    def test_namespaces_and_owner_protection(self) -> None:
        first = json.loads(
            self.run_cli("start", "10m", "--key", "job", "--namespace", "one", "--owner", "alice", "--json").stdout
        )
        second = json.loads(
            self.run_cli("start", "10m", "--key", "job", "--namespace", "two", "--owner", "bob", "--json").stdout
        )
        wrong_owner = json.loads(
            self.run_cli("cancel", "--key", "job", "--namespace", "one", "--owner", "bob", "--json").stdout
        )
        forced = json.loads(
            self.run_cli("cancel", "--key", "job", "--namespace", "one", "--owner", "bob", "--force", "--json").stdout
        )

        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(json.loads(self.run_cli("list", "--all", "--namespace", "one", "--json").stdout)[0]["id"], first["id"])
        self.assertEqual(wrong_owner["error"]["code"], "owner_mismatch")
        self.assertEqual(forced["event"], "cancelled")

    def test_wait_any_and_wait_all_use_stable_timer_sets(self) -> None:
        self.run_cli("start", "0.05s", "--key", "cook-rice")
        self.run_cli("start", "0.12s", "--key", "cook-beans")

        first = json.loads(self.run_cli("wait", "--any", "cook-rice,cook-beans", "--json").stdout)
        all_done = json.loads(self.run_cli("wait", "--all", "--key-prefix", "cook-", "--json").stdout)

        self.assertEqual(first["key"], "cook-rice")
        self.assertEqual(first["event"], "expired")
        self.assertEqual(all_done["event"], "all_complete")
        self.assertEqual({item["key"] for item in all_done["timers"]}, {"cook-rice", "cook-beans"})
        self.assertTrue(all(item["status"] == "expired" for item in all_done["timers"]))

    def test_recurring_schedule_emits_ticks_and_can_be_cancelled(self) -> None:
        started = json.loads(
            self.run_cli(
                "every", "0.05s", "--until", "1s", "--key", "deploy",
                "--message", "Read deployment status", "--json",
            ).stdout
        )
        retried = json.loads(
            self.run_cli("every", "0.05s", "--until", "1s", "--key", "deploy", "--json").stdout
        )
        time.sleep(0.12)
        ticks = json.loads(self.run_cli("pending", "--event", "tick", "--json").stdout)
        cancelled = json.loads(self.run_cli("cancel", "--key", "deploy", "--json").stdout)

        self.assertEqual(started["id"], retried["id"])
        self.assertEqual(retried["event"], "existing")
        self.assertGreaterEqual(len(ticks), 2)
        self.assertEqual([event["tick"] for event in ticks], list(range(1, len(ticks) + 1)))
        self.assertTrue(all(event["message"] == "Read deployment status" for event in ticks))
        self.assertEqual(cancelled["event"], "cancelled")
        self.assertEqual(cancelled["status"], "cancelled")

    def test_recurring_series_is_visible_and_pauses_at_unacknowledged_limit(self) -> None:
        self.run_cli(
            "every", "0.02s", "--until", "1s", "--key", "rollup",
            "--max-unacked", "2", "--json",
        )
        self.assertIn("rollup: every", self.run_cli().stdout)
        time.sleep(0.08)

        listed = json.loads(self.run_cli("list", "--json").stdout)
        series = next(item for item in listed if item["key"] == "rollup")
        direct = json.loads(self.run_cli("status", "--key", "rollup", "--json").stdout)

        self.assertEqual(series["kind"], "series")
        self.assertEqual(series["status"], "paused")
        self.assertEqual(series["unacknowledged_ticks"], 2)
        self.assertEqual(series["max_unacknowledged_ticks"], 2)
        self.assertEqual(direct["id"], series["id"])

        claimed = json.loads(
            self.run_cli(
                "claim", "--consumer", "test-worker", "--event", "tick", "--json"
            ).stdout
        )
        self.run_cli(
            "ack", claimed["delivery"]["event_id"], "--consumer", "test-worker",
            "--lease-id", claimed["lease_id"], "--json",
        )
        resumed = json.loads(self.run_cli("status", "--key", "rollup", "--json").stdout)
        self.assertEqual(resumed["status"], "active")
        self.assertEqual(resumed["unacknowledged_ticks"], 1)

    def test_stopwatch_lap_is_available_as_an_event(self) -> None:
        self.run_cli("stopwatch", "start", "prep")
        self.run_cli("stopwatch", "lap", "prep")
        events = json.loads(self.run_cli("pending", "--event", "lap", "--json").stdout)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "lap")
        self.assertEqual(events[0]["key"], "prep")
        self.assertEqual(events[0]["lap"]["number"], 1)

    def test_json_follow_streams_ndjson_without_terminal_codes(self) -> None:
        watcher = subprocess.Popen(
            [str(CLI), "watch", "--json", "--follow", "--interval", "0.02"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        time.sleep(0.08)
        self.run_cli("start", "0.05s", "--key", "streamed")
        self.run_cli("wait", "--key", "streamed", "--json")
        time.sleep(0.08)
        watcher.send_signal(signal.SIGINT)
        stdout, stderr = watcher.communicate(timeout=2)

        events = [json.loads(line) for line in stdout.splitlines() if line]
        self.assertEqual(watcher.returncode, 0, stderr)
        self.assertNotIn("\033", stdout)
        self.assertIn("started", {event["event"] for event in events})
        self.assertIn("expired", {event["event"] for event in events})
        self.assertTrue(all(event["schema"] == "timer.event.v1" for event in events))

    def test_claim_ack_nack_and_consumer_isolation(self) -> None:
        self.run_cli("start", "0.05s", "--key", "claimable", "--message", "Continue")
        self.run_cli("wait", "--key", "claimable", "--json")

        first = json.loads(
            self.run_cli("claim", "--consumer", "agent:a", "--lease", "1s", "--event", "expired", "--json").stdout
        )
        busy = json.loads(
            self.run_cli("claim", "--consumer", "agent:a", "--lease", "1s", "--event", "expired", "--json").stdout
        )
        independent = json.loads(
            self.run_cli("claim", "--consumer", "agent:b", "--lease", "1s", "--event", "expired", "--json").stdout
        )
        event_id = first["delivery"]["event_id"]
        acked = json.loads(
            self.run_cli(
                "ack", event_id, "--consumer", "agent:a", "--lease-id", first["lease_id"], "--json"
            ).stdout
        )
        empty = json.loads(
            self.run_cli("claim", "--consumer", "agent:a", "--lease", "1s", "--event", "expired", "--json").stdout
        )
        nacked = json.loads(
            self.run_cli(
                "nack", event_id, "--consumer", "agent:b", "--lease-id", independent["lease_id"], "--json"
            ).stdout
        )
        redelivered = json.loads(
            self.run_cli("claim", "--consumer", "agent:b", "--lease", "1s", "--event", "expired", "--json").stdout
        )

        self.assertEqual(first["event"], "claimed")
        self.assertEqual(busy["event"], "busy")
        self.assertEqual(independent["delivery"]["event_id"], event_id)
        self.assertEqual(acked["event"], "acked")
        self.assertEqual(empty["event"], "empty")
        self.assertEqual(nacked["event"], "nacked")
        self.assertEqual(redelivered["delivery"]["event_id"], event_id)

    def test_consumers_forget_and_compaction_blocker_are_visible(self) -> None:
        self.run_cli("start", "10m", "--key", "retained")
        self.run_cli("pending", "--consumer", "forgotten-path", "--json")

        listed = json.loads(self.run_cli("consumers", "--json").stdout)
        blocked = json.loads(self.run_cli("compact", "--json").stdout)
        forgotten = json.loads(self.run_cli("forget", "forgotten-path", "--json").stdout)

        self.assertEqual(listed["consumers"][0]["name"], "forgotten-path")
        self.assertGreater(listed["consumers"][0]["lag_bytes"], 0)
        self.assertEqual(blocked["error"]["code"], "compaction_blocked")
        self.assertIn("forgotten-path", blocked["error"]["message"])
        self.assertEqual(forgotten["event"], "forgotten")
        self.assertEqual(json.loads(self.run_cli("consumers", "--json").stdout)["consumers"], [])

    def test_forget_refuses_active_lease_without_force(self) -> None:
        self.run_cli("start", "0.02s", "--key", "leased-consumer")
        self.run_cli("wait", "--key", "leased-consumer", "--json")
        self.run_cli(
            "claim", "--consumer", "busy-consumer", "--event", "expired", "--lease", "5m", "--json"
        )

        refused = json.loads(self.run_cli("forget", "busy-consumer", "--json").stdout)
        forced = json.loads(self.run_cli("forget", "busy-consumer", "--force", "--json").stdout)

        self.assertEqual(refused["error"]["code"], "consumer_busy")
        self.assertEqual(forced["event"], "forgotten")

    def test_daemon_allows_only_one_process_per_namespace(self) -> None:
        first = subprocess.Popen(
            [str(CLI), "daemon", "--poll-interval", "0.05"],
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self.env,
        )
        try:
            time.sleep(0.1)
            second = json.loads(self.run_cli("daemon", "--once", "--json").stdout)
            self.assertEqual(second["error"]["code"], "busy")
            self.assertIn("daemon already running", second["error"]["message"])
        finally:
            first.send_signal(signal.SIGINT)
            _, stderr = first.communicate(timeout=2)
            self.assertEqual(first.returncode, 0, stderr)

    def make_service_fakes(self, platform: str) -> tuple[Path, Path]:
        fake_dir = Path(self.temp.name) / "bin"
        fake_dir.mkdir()
        codex = fake_dir / "codex"
        supervisor = fake_dir / "timer-supervisor"
        controller = fake_dir / ("launchctl" if platform == "launchd" else "systemctl")
        calls = Path(self.temp.name) / "service-calls.txt"
        codex.write_text(
            "#!/bin/sh\n"
            "if [ \"$1\" = queue ] && [ \"$2\" = --help ]; then\n"
            "  printf '%s\\n' 'Usage: codex queue --thread ID' 'Session UUID or exact session name'\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        supervisor.write_text(
            "#!/bin/sh\nprintf '%s\\n' '{\"schema\":\"timer.next-turn.v1\"}'\n",
            encoding="utf-8",
        )
        if platform == "launchd":
            controller.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TIMER_TEST_CALLS"\n'
                'exit 0\n',
                encoding="utf-8",
            )
            self.env["TIMER_LAUNCHCTL_BIN"] = str(controller)
        else:
            controller.write_text(
                '#!/bin/sh\nprintf "%s\\n" "$*" >> "$TIMER_TEST_CALLS"\nexit 0\n',
                encoding="utf-8",
            )
            self.env["TIMER_SYSTEMCTL_BIN"] = str(controller)
        for executable in (codex, supervisor, controller):
            executable.chmod(0o700)
        self.env["TIMER_CODEX_BIN"] = str(codex)
        self.env["TIMER_SUPERVISOR_BIN"] = str(supervisor)
        self.env["TIMER_SERVICE_PLATFORM"] = platform
        self.env["TIMER_TEST_CALLS"] = str(calls)
        return calls, supervisor

    def test_setup_dry_run_checks_dependencies_without_installing(self) -> None:
        self.make_service_fakes("launchd")
        service_path = Path(self.temp.name) / "LaunchAgents" / "timer.plist"
        self.env["TIMER_SERVICE_PATH"] = str(service_path)

        result = json.loads(self.run_cli("setup", "--dry-run", "--json").stdout)

        self.assertEqual(result["event"], "validated")
        self.assertEqual(result["checks"], ["codex_queue", "synthetic_expiry"])
        self.assertFalse(service_path.exists())

    def test_setup_rejects_queue_without_session_capable_thread_flag(self) -> None:
        self.make_service_fakes("launchd")
        codex = Path(self.env["TIMER_CODEX_BIN"])
        codex.write_text("#!/bin/sh\nprintf '%s\\n' 'Usage: codex queue --thread ID'\n", encoding="utf-8")

        result = json.loads(self.run_cli("setup", "--dry-run", "--json").stdout)

        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "preflight_failed")

    def test_service_status_is_stalled_when_running_hook_consumer_has_failed(self) -> None:
        _, supervisor = self.make_service_fakes("launchd")
        service_path = Path(self.temp.name) / "LaunchAgents" / "timer.plist"
        self.env["TIMER_SERVICE_PATH"] = str(service_path)
        self.run_cli("setup", "--json")
        supervisor.write_text("#!/bin/sh\nexit 7\n", encoding="utf-8")
        self.run_cli("start", "0.05s", "--key", "stalled")
        time.sleep(0.08)
        self.run_cli("daemon", "--once", "--hook", str(supervisor))

        status = json.loads(self.run_cli("service", "status", "--json").stdout)

        self.assertEqual(status["event"], "stalled")
        self.assertTrue(status["running"])
        self.assertGreater(status["consumer"]["lag_bytes"], 0)
        self.assertEqual(status["consumer"]["last_error"]["code"], "hook_failed")

    def test_service_status_is_stalled_when_lag_grows_while_process_runs(self) -> None:
        self.make_service_fakes("launchd")
        service_path = Path(self.temp.name) / "LaunchAgents" / "timer.plist"
        self.env["TIMER_SERVICE_PATH"] = str(service_path)
        self.run_cli("setup", "--json")
        healthy = json.loads(self.run_cli("service", "status", "--json").stdout)
        self.run_cli("start", "10m", "--key", "new-lag")

        stalled = json.loads(self.run_cli("service", "status", "--json").stdout)

        self.assertEqual(healthy["event"], "delivering")
        self.assertEqual(stalled["event"], "stalled")
        self.assertTrue(stalled["running"])
        self.assertIn("lag_not_shrinking", stalled["consumer"]["stall_reasons"])

    def test_service_status_is_stalled_when_delivery_lease_is_stuck(self) -> None:
        _, supervisor = self.make_service_fakes("launchd")
        service_path = Path(self.temp.name) / "LaunchAgents" / "timer.plist"
        self.env["TIMER_SERVICE_PATH"] = str(service_path)
        self.run_cli("setup", "--json")
        registry_path = Path(self.temp.name) / "consumers.json"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        record = registry["consumers"][f"daemon-hook:{supervisor.resolve()}"]
        record["lease"] = {
            "lease_id": "stuck-lease",
            "event_id": "stuck-event",
            "offset": record["cursor"],
            "next_offset": record["cursor"],
            "lease_started_at": time.time() - 10,
            "lease_until": time.time() + 60,
        }
        registry_path.write_text(json.dumps(registry), encoding="utf-8")

        status = json.loads(
            self.run_cli(
                "service", "status", "--delivery-lease", "1", "--json"
            ).stdout
        )

        self.assertEqual(status["event"], "stalled")
        self.assertTrue(status["consumer"]["lease_stuck"])
        self.assertEqual(status["consumer"]["leased_event_id"], "stuck-event")

    def test_setup_installs_launchd_service_and_seeds_log_tail(self) -> None:
        calls, supervisor = self.make_service_fakes("launchd")
        service_path = Path(self.temp.name) / "LaunchAgents" / "timer.plist"
        self.env["TIMER_SERVICE_PATH"] = str(service_path)
        self.run_cli("start", "10m", "--key", "old-event")

        result = json.loads(self.run_cli("setup", "--json").stdout)
        consumers = json.loads(self.run_cli("consumers", "--json").stdout)["consumers"]
        health = json.loads(self.run_cli("service", "status", "--json").stdout)

        self.assertEqual(result["event"], "installed")
        self.assertTrue(service_path.exists())
        self.assertIn("bootstrap", calls.read_text(encoding="utf-8"))
        self.assertIn("PATH", service_path.read_text(encoding="utf-8"))
        self.assertEqual(consumers[0]["name"], f"daemon-hook:{supervisor.resolve()}")
        self.assertEqual(consumers[0]["lag_bytes"], 0)
        self.assertEqual(health["event"], "delivering")

    def test_setup_generates_and_activates_linux_user_service(self) -> None:
        calls, supervisor = self.make_service_fakes("systemd")
        service_path = Path(self.temp.name) / "systemd" / "timer-supervisor.service"
        self.env["TIMER_SERVICE_PATH"] = str(service_path)

        result = json.loads(self.run_cli("setup", "--json").stdout)
        unit = service_path.read_text(encoding="utf-8")

        self.assertEqual(result["platform"], "systemd")
        self.assertIn(str(supervisor.absolute()), unit)
        self.assertIn("Environment=\"PATH=", unit)
        self.assertIn("--user daemon-reload", calls.read_text(encoding="utf-8"))
        self.assertIn("--user enable --now timer-supervisor.service", calls.read_text(encoding="utf-8"))

    def test_help_and_schema_show_the_safe_agent_recipe(self) -> None:
        help_text = self.run_cli("--help").stdout
        drain_help = self.run_cli("drain", "--help").stdout
        schema = json.loads(self.run_cli("schema", "--json").stdout)

        self.assertIn("Agent recipe", help_text)
        self.assertIn("scripts only", help_text)
        self.assertIn("acknowledge events before caller work", drain_help)
        self.assertIn("start", schema["agent_recipe"][0])
        self.assertIn("claim", schema["agent_recipe"][1])
        self.assertIn("ack", schema["agent_recipe"][2])
        self.assertIn("acknowledges each event before caller work", schema["warnings"]["drain"])

    def test_expired_lease_redelivers_same_event(self) -> None:
        self.run_cli("start", "0.05s", "--key", "lease")
        self.run_cli("wait", "--key", "lease", "--json")
        first = json.loads(
            self.run_cli("claim", "--consumer", "agent:lease", "--lease", "0.05s", "--event", "expired", "--json").stdout
        )
        time.sleep(0.08)
        second = json.loads(
            self.run_cli("claim", "--consumer", "agent:lease", "--lease", "1s", "--event", "expired", "--json").stdout
        )

        self.assertEqual(first["delivery"]["event_id"], second["delivery"]["event_id"])
        self.assertNotEqual(first["lease_id"], second["lease_id"])

        stale = json.loads(
            self.run_cli(
                "nack", first["delivery"]["event_id"], "--consumer", "agent:lease",
                "--lease-id", first["lease_id"], "--json",
            ).stdout
        )
        busy = json.loads(
            self.run_cli(
                "claim", "--consumer", "agent:lease", "--lease", "1s", "--event", "expired", "--json"
            ).stdout
        )

        self.assertEqual(stale["error"]["code"], "lease_mismatch")
        self.assertEqual(busy["event"], "busy")

    def test_compaction_stops_at_oldest_consumer_cursor(self) -> None:
        expired_ids = []
        for index in range(3):
            key = f"compact-{index}"
            self.run_cli("start", "0.02s", "--key", key)
            event = json.loads(self.run_cli("wait", "--key", key, "--json").stdout)
            expired_ids.append(event["event_id"])

        for _ in range(3):
            claimed = json.loads(
                self.run_cli("claim", "--consumer", "fast", "--event", "expired", "--json").stdout
            )
            self.run_cli(
                "ack", claimed["delivery"]["event_id"], "--consumer", "fast",
                "--lease-id", claimed["lease_id"], "--json",
            )
        slow = json.loads(
            self.run_cli("claim", "--consumer", "slow", "--event", "expired", "--json").stdout
        )
        self.run_cli(
            "ack", slow["delivery"]["event_id"], "--consumer", "slow",
            "--lease-id", slow["lease_id"], "--json",
        )

        compacted = json.loads(self.run_cli("compact", "--json").stdout)
        next_slow = json.loads(
            self.run_cli("claim", "--consumer", "slow", "--event", "expired", "--json").stdout
        )
        remaining = json.loads(self.run_cli("list", "--all", "--json").stdout)

        self.assertGreater(compacted["bytes_removed"], 0)
        self.assertEqual(compacted["events_removed"], 2)
        self.assertEqual(compacted["timers_pruned"], 1)
        self.assertEqual(next_slow["delivery"]["event_id"], expired_ids[1])
        self.assertEqual({item["key"] for item in remaining}, {"compact-1", "compact-2"})

    def test_shared_event_log_uses_one_consumer_registry_for_compaction(self) -> None:
        shared_events = Path(self.temp.name) / "shared" / "events.jsonl"
        self.env["TIMER_EVENTS"] = str(shared_events)
        self.run_cli("start", "0.05s", "--key", "shared-log")
        expired = json.loads(self.run_cli("wait", "--key", "shared-log", "--json").stdout)
        slow = json.loads(
            self.run_cli("claim", "--consumer", "slow-shared", "--event", "expired", "--json").stdout
        )
        fast = json.loads(
            self.run_cli("claim", "--consumer", "fast-shared", "--event", "expired", "--json").stdout
        )
        self.run_cli(
            "ack", fast["delivery"]["event_id"], "--consumer", "fast-shared",
            "--lease-id", fast["lease_id"], "--json",
        )

        original_state = self.env["TIMER_STATE"]
        self.env["TIMER_STATE"] = str(Path(self.temp.name) / "second-state.json")
        compacted = json.loads(self.run_cli("compact", "--json").stdout)
        self.run_cli(
            "nack", slow["delivery"]["event_id"], "--consumer", "slow-shared",
            "--lease-id", slow["lease_id"], "--json",
        )
        reclaimed = json.loads(
            self.run_cli("claim", "--consumer", "slow-shared", "--event", "expired", "--json").stdout
        )
        self.env["TIMER_STATE"] = original_state

        self.assertEqual(compacted["events_removed"], 1)
        self.assertEqual(reclaimed["delivery"]["event_id"], expired["event_id"])

    def test_interrupted_compaction_rebases_cursors_from_journal(self) -> None:
        expired_ids = []
        for index in range(2):
            key = f"recover-{index}"
            self.run_cli("start", "0.02s", "--key", key)
            event = json.loads(self.run_cli("wait", "--key", key, "--json").stdout)
            expired_ids.append(event["event_id"])
        first = json.loads(
            self.run_cli("claim", "--consumer", "recovering", "--event", "expired", "--json").stdout
        )
        self.run_cli(
            "ack", first["delivery"]["event_id"], "--consumer", "recovering",
            "--lease-id", first["lease_id"], "--json",
        )

        registry_path = Path(self.temp.name) / "consumers.json"
        event_path = Path(self.temp.name) / "events.jsonl"
        registry = json.loads(registry_path.read_text(encoding="utf-8"))
        cutoff = registry["consumers"]["recovering"]["cursor"]
        old_events = event_path.read_bytes()
        remainder = old_events[cutoff:]
        state_after = json.loads(json.dumps(registry))
        state_after["generation"] = registry.get("generation", 0) + 1
        state_after["consumers"]["recovering"]["cursor"] = 0
        journal = {
            "schema": "timer.compaction-journal.v1",
            "old_event_digest": hashlib.sha256(old_events).hexdigest(),
            "old_event_size": len(old_events),
            "new_event_digest": hashlib.sha256(remainder).hexdigest(),
            "new_event_size": len(remainder),
            "target_generation": state_after["generation"],
            "consumer_state_after": state_after,
        }
        event_path.write_bytes(remainder)
        registry_path.with_name("consumers.json.compact-journal").write_text(
            json.dumps(journal), encoding="utf-8"
        )

        recovered = json.loads(
            self.run_cli("claim", "--consumer", "recovering", "--event", "expired", "--json").stdout
        )

        self.assertEqual(recovered["delivery"]["event_id"], expired_ids[1])
        self.assertFalse(registry_path.with_name("consumers.json.compact-journal").exists())

    def test_legacy_acknowledgements_seed_default_consumer_cursor(self) -> None:
        self.run_cli("start", "0.05s", "--key", "already-drained")
        expired = json.loads(self.run_cli("wait", "--key", "already-drained", "--json").stdout)
        state_path = Path(self.env["TIMER_STATE"])
        state = json.loads(state_path.read_text(encoding="utf-8"))
        state["acked_event_ids"] = [expired["event_id"]]
        state_path.write_text(json.dumps(state), encoding="utf-8")

        pending = json.loads(self.run_cli("pending", "--json").stdout)

        self.assertEqual([event["event"] for event in pending], ["started"])

    def test_structured_payload_and_reference_convention(self) -> None:
        payload = {
            "goal": "Check deploy",
            "done_when": "success or failed",
            "artifacts": ["logs/deploy.txt"],
            "budget": {"ticks_left": 12},
        }
        self.run_cli(
            "start", "0.05s", "--key", "payload", "--message", "Continue deploy",
            "--ref", "thread:abc-123", "--payload", json.dumps(payload), "--json",
        )
        event = json.loads(self.run_cli("wait", "--key", "payload", "--json").stdout)
        invalid = json.loads(
            self.run_cli("start", "1m", "--key", "bad-ref", "--ref", "opaque", "--json").stdout
        )

        self.assertEqual(event["payload"], payload)
        self.assertEqual(event["ref"], "thread:abc-123")
        self.assertEqual(invalid["error"]["code"], "invalid")

    def test_payload_file_is_preserved_in_expiration_event(self) -> None:
        payload_file = Path(self.temp.name) / "work.json"
        payload_file.write_text('{"action":"check_build","attempt":2}\n', encoding="utf-8")
        self.run_cli(
            "start", "0.05s", "--key", "payload-file", "--ref", "task:build-2",
            "--payload-file", str(payload_file), "--json",
        )

        event = json.loads(self.run_cli("wait", "--key", "payload-file", "--json").stdout)

        self.assertEqual(event["payload"], {"action": "check_build", "attempt": 2})

    def test_active_lease_prevents_compaction_past_claimed_event(self) -> None:
        self.run_cli("start", "0.05s", "--key", "leased")
        self.run_cli("wait", "--key", "leased")
        claimed = json.loads(
            self.run_cli(
                "claim", "--consumer", "worker", "--event", "expired", "--lease", "5m", "--json"
            ).stdout
        )

        compacted = json.loads(self.run_cli("compact", "--json").stdout)
        self.run_cli(
            "nack", claimed["delivery"]["event_id"], "--consumer", "worker",
            "--lease-id", claimed["lease_id"], "--json",
        )
        reclaimed = json.loads(
            self.run_cli(
                "claim", "--consumer", "worker", "--event", "expired", "--lease", "5m", "--json"
            ).stdout
        )

        self.assertEqual(claimed["event"], "claimed")
        self.assertEqual(compacted["events_removed"], 1)
        self.assertEqual(reclaimed["delivery"]["event_id"], claimed["delivery"]["event_id"])

    def test_reference_supervisor_builds_and_queues_next_turn(self) -> None:
        event = {
            "ok": True,
            "schema": "timer.event.v1",
            "event_id": "event-1",
            "event": "expired",
            "id": "timer-1",
            "key": "deploy",
            "timestamp": "2026-09-15T10:00:00-04:00",
            "namespace": "default",
            "owner": "codex",
            "ref": "thread:thread-123",
            "message": "Check the deploy",
            "payload": {"goal": "Intervene only on failure"},
        }
        dry_run = json.loads(self.run_supervisor(event, "--dry-run").stdout)
        fake_codex = Path(self.temp.name) / "codex"
        captured = Path(self.temp.name) / "codex-args.txt"
        fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$SUPERVISOR_ARGS"\n', encoding="utf-8")
        fake_codex.chmod(0o700)
        self.env["SUPERVISOR_ARGS"] = str(captured)
        queued = json.loads(self.run_supervisor(event, "--codex-bin", str(fake_codex)).stdout)
        arguments = captured.read_text(encoding="utf-8").splitlines()

        self.assertEqual(dry_run["schema"], "timer.next-turn.v1")
        self.assertEqual(dry_run["route"], {"kind": "thread", "id": "thread-123"})
        self.assertEqual(dry_run["work"]["goal"], "Intervene only on failure")
        self.assertEqual(arguments[:3], ["queue", "--thread", "thread-123"])
        self.assertEqual(arguments[3], "--message")
        self.assertIn("timer.next-turn.v1", "\n".join(arguments[4:]))
        self.assertEqual(queued["event"], "queued")

    def test_reference_supervisor_routes_session_through_session_capable_thread_flag(self) -> None:
        event = {
            "schema": "timer.event.v1",
            "event_id": "event-session",
            "event": "expired",
            "id": "timer-session",
            "key": "session-check",
            "timestamp": "2026-09-16T08:00:00-04:00",
            "ref": "session:session-123",
        }
        fake_codex = Path(self.temp.name) / "codex"
        captured = Path(self.temp.name) / "session-args.txt"
        fake_codex.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$SUPERVISOR_ARGS"\n', encoding="utf-8")
        fake_codex.chmod(0o700)
        self.env["SUPERVISOR_ARGS"] = str(captured)

        result = json.loads(self.run_supervisor(event, "--codex-bin", str(fake_codex)).stdout)
        arguments = captured.read_text(encoding="utf-8").splitlines()

        self.assertEqual(result["event"], "queued")
        self.assertEqual(arguments[:3], ["queue", "--thread", "session-123"])

    def test_reference_supervisor_rejects_malformed_route_without_traceback(self) -> None:
        event = {
            "schema": "timer.event.v1",
            "event_id": "event-1",
            "event": "expired",
            "key": "deploy",
            "payload": {"route_ref": {"thread": "abc"}},
        }

        result = self.run_supervisor(event, "--dry-run", expected=2)

        self.assertIn("route references must be non-empty strings", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

        top_level = self.run_supervisor([], "--dry-run", expected=2)  # type: ignore[arg-type]
        self.assertIn("JSON object", top_level.stderr)
        self.assertNotIn("Traceback", top_level.stderr)


if __name__ == "__main__":
    unittest.main()
