from __future__ import annotations

import json
import os
import signal
import subprocess
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
        for name in ("TIMER_EVENTS", "TIMER_NAMESPACE", "TIMER_OWNER"):
            self.env.pop(name, None)
        self.env["TIMER_STATE"] = str(Path(self.temp.name) / "timers.json")

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_cli(self, *args: str, expected: int = 0) -> subprocess.CompletedProcess[str]:
        result = subprocess.run(
            [str(CLI), *args], text=True, capture_output=True, env=self.env, timeout=3
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


if __name__ == "__main__":
    unittest.main()
