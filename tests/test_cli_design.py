#!/usr/bin/env python3
"""Tests for redesigned CLI UX, filtering, show, prune, poller status, and stream hygiene."""

import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from cubrid_ci_watch.collector import resolve_snapshot_executable  # noqa: E402
from cubrid_ci_watch.format import resolve_color  # noqa: E402
from cubrid_ci_watch.store import connect, inspect_poller  # noqa: E402

HEAD = "a" * 40
BASE = "https://github.com/CUBRID/cubrid"


class CliDesignTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.state = self.root / "state"
        self.fixture = self.root / "fixture.json"
        fake = self.root / "fake"
        fake.write_text(
            """#!/usr/bin/env python3
import json, os, sys
data = json.load(open(os.environ['FIXTURE']))
if sys.argv[0].endswith('snapshot'):
    pr_num = int(sys.argv[-1].rsplit('/', 1)[-1])
    snap = dict(data['snapshot'])
    snap['pr'] = {'number': pr_num, 'url': f'https://github.com/CUBRID/cubrid/pull/{pr_num}', 'head_sha': data['snapshot']['pr']['head_sha']}
    print(json.dumps(snap))
elif '/jobs?' in sys.argv[-1]:
    print(json.dumps(data['jobs']))
else:
    print(json.dumps(data['run']))
"""
        )
        fake.chmod(0o755)
        (self.root / "snapshot").symlink_to(fake)
        (self.root / "gh").symlink_to(fake)
        existing_pp = os.environ.get("PYTHONPATH", "")
        pp = f"{SRC_DIR}{os.pathsep}{existing_pp}" if existing_pp else str(SRC_DIR)
        self.env = dict(
            os.environ,
            PYTHONPATH=pp,
            FIXTURE=str(self.fixture),
            CUBRID_CI_WATCH_SNAPSHOT=str(self.root / "snapshot"),
            CUBRID_CI_WATCH_GH=str(self.root / "gh"),
        )
        self.when = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
        self.write_fixture()

    def tearDown(self):
        self.tmp.cleanup()

    def write_fixture(self, states=None):
        states = states or {}
        current = (self.when - dt.timedelta(minutes=5)).isoformat()
        checks = [
            {
                "name": "gha-ci: " + name,
                "current": {
                    "state": state,
                    "reported_for_sha": HEAD,
                    "detail_url": BASE + "/actions/runs/123",
                    "reported_at": current,
                },
            }
            for name, state in states.items()
        ]
        jobs = [
            {
                "name": name,
                "started_at": (self.when - dt.timedelta(minutes=6)).isoformat(),
                "completed_at": (self.when - dt.timedelta(minutes=4)).isoformat(),
                "status": "completed",
                "conclusion": "success",
            }
            for name in ("build status", "sql / status sql", "shell / status shell")
        ]
        self.fixture.write_text(
            json.dumps(
                {
                    "snapshot": {
                        "schema_version": 1,
                        "repository": "CUBRID/cubrid",
                        "complete": True,
                        "errors": [],
                        "pr": {"number": 100, "url": BASE + "/pull/100", "head_sha": HEAD},
                        "checks": checks,
                    },
                    "run": {
                        "id": 123,
                        "run_attempt": 1,
                        "path": ".github/workflows/gha-ci.yml",
                        "created_at": (self.when - dt.timedelta(minutes=9)).isoformat(),
                    },
                    "jobs": [{"jobs": jobs}],
                }
            )
        )

    def run_cli(self, *args, expected=0, state_dir_after=False, env_extra=None):
        if state_dir_after:
            cmd = [sys.executable, "-m", "cubrid_ci_watch", *args, "--state-dir", str(self.state)]
        else:
            cmd = [sys.executable, "-m", "cubrid_ci_watch", "--state-dir", str(self.state), *args]
        env = dict(self.env, **(env_extra or {}))
        proc = subprocess.run(cmd, env=env, text=True, capture_output=True, timeout=15)
        self.assertEqual(proc.returncode, expected, proc.stderr)
        return proc.stdout, proc.stderr

    def register(self, pr=100, days=0, receipt="r1", suite="sql"):
        triggered = (self.when - dt.timedelta(days=days, minutes=10)).isoformat()
        out, _ = self.run_cli(
            "register",
            str(pr),
            "--head",
            HEAD,
            "--suite",
            suite,
            "--triggered-at",
            triggered,
            "--receipt",
            receipt,
            "--run-id",
            "123",
            "--attempt",
            "1",
        )
        return json.loads(out)["request_id"]

    def seed_mixed_ledger(self):
        # #1 closed (40 days ago, success)
        r1 = self.register(pr=101, days=40, receipt="old-success")
        self.write_fixture({"build (debug)": "SUCCESS", "build (release)": "SUCCESS", "test_sql": "SUCCESS"})
        self.run_cli("poll", "--once")
        # #2 closed (10 days ago, failure)
        r2 = self.register(pr=102, days=10, receipt="recent-fail")
        self.write_fixture({"build (debug)": "FAILURE", "build (release)": "SUCCESS", "test_sql": "SUCCESS"})
        self.run_cli("poll", "--once")
        # #3 active (today, pending)
        r3 = self.register(pr=103, days=0, receipt="active-1")
        self.write_fixture({"build (debug)": "SUCCESS", "build (release)": "SUCCESS", "test_sql": "PENDING"})
        self.run_cli("poll", "--once")
        # #4 active (today, no observation yet)
        r4 = self.register(pr=104, days=0, receipt="active-2", suite="shell")
        return r1, r2, r3, r4

    def test_default_status_shows_only_active_newest_first_and_zero_ansi(self):
        self.seed_mixed_ledger()
        out, _ = self.run_cli("status")
        self.assertNotIn("\x1b[", out)
        self.assertIn("poller: stopped | active: 2, closed: 2", out)
        self.assertIn("\n#4  #104", out)
        self.assertIn("\n#3  #103", out)
        self.assertNotIn("\n#1  ", out)
        self.assertNotIn("\n#2  ", out)
        self.assertNotIn("#101", out)
        self.assertNotIn("#102", out)
        # Newest active (#4) appears before older active (#3)
        self.assertLess(out.index("\n#4  "), out.index("\n#3  "))

        # Alias `ls` and `--state-dir` placed after subcommand work identically
        out_ls, _ = self.run_cli("ls", state_dir_after=True)
        self.assertEqual(out, out_ls)

    def test_status_filters_plain_and_json_backward_compatibility(self):
        self.seed_mixed_ledger()

        # Unfiltered --json preserves all requests in ascending ID order
        raw_all, _ = self.run_cli("status", "--json")
        data_all = json.loads(raw_all)
        self.assertEqual(data_all["schema_version"], 1)
        self.assertEqual([r["id"] for r in data_all["requests"]], [1, 2, 3, 4])

        # Filtered --json narrows results
        raw_active, _ = self.run_cli("status", "--active", "--json")
        self.assertEqual([r["id"] for r in json.loads(raw_active)["requests"]], [3, 4])

        raw_recent, _ = self.run_cli("status", "--recent", "2", "--json")
        self.assertEqual([r["id"] for r in json.loads(raw_recent)["requests"]], [4, 3])

        # Human filters: --closed, --outcome, --pr, --recent, --all, -v, --plain
        closed_out, _ = self.run_cli("status", "--closed", "--outcome", "failure")
        self.assertIn("\n#2  #102", closed_out)
        self.assertNotIn("\n#1  ", closed_out)
        self.assertNotIn("\n#3  ", closed_out)
        self.assertNotIn("#101", closed_out)
        self.assertNotIn("#103", closed_out)

        pr_out, _ = self.run_cli("status", "--pr", "103", "-v")
        self.assertIn("\n#3  #103", pr_out)
        self.assertIn("test_sql: PENDING (attributed)", pr_out)

        all_out, _ = self.run_cli("status", "--all")
        for line_prefix in ("\n#1  #101", "\n#2  #102", "\n#3  #103", "\n#4  #104"):
            self.assertIn(line_prefix, all_out)

        plain_out, _ = self.run_cli("status", "--recent", "2", "--plain")
        lines = plain_out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[0].startswith("4\t104\tactive\t-\t123\t1\tshell\t"))
        self.assertTrue(lines[1].startswith("3\t103\tactive\t-\t123\t1\tsql\t"))

    def test_empty_active_status_is_one_concise_message(self):
        self.register(pr=101, days=1, receipt="only-closed")
        self.write_fixture({"build (debug)": "SUCCESS", "build (release)": "SUCCESS", "test_sql": "SUCCESS"})
        self.run_cli("poll", "--once")
        out, _ = self.run_cli("status")
        self.assertIn("No active requests.", out)
        self.assertIn("1 closed", out)

    def test_show_detail_and_unknown_request(self):
        _, _, r3, _ = self.seed_mixed_ledger()
        out, _ = self.run_cli("show", str(r3))
        self.assertIn("#3 PR #103 — active", out)
        self.assertIn("Head SHA:", out)
        self.assertIn("Contexts:", out)
        self.assertIn("test_sql", out)
        self.assertIn("Events (", out)

        raw, _ = self.run_cli("show", str(r3), "--json")
        payload = json.loads(raw)
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(payload["request"]["id"], r3)
        self.assertGreaterEqual(len(payload["events"]), 2)

        _, err = self.run_cli("show", "9999", expected=2)
        self.assertIn("unknown request 9999", err)

    def test_events_filters(self):
        _, r2, _, _ = self.seed_mixed_ledger()
        out, _ = self.run_cli("events", "--request", str(r2), "--kind", "failure")
        evs = [json.loads(line) for line in out.splitlines()]
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]["request_id"], r2)
        self.assertEqual(evs[0]["kind"], "failure")

        limited, _ = self.run_cli("events", "--limit", "2")
        self.assertEqual(len(limited.splitlines()), 2)

    def test_prune_dry_run_default_yes_and_monotonic_events(self):
        r1, r2, r3, r4 = self.seed_mixed_ledger()

        # Default prune (no --yes) is dry-run: deletes nothing
        dry_out, _ = self.run_cli("prune", "--older-than", "30d")
        self.assertIn("Dry run: would delete 1 closed request(s)", dry_out)
        self.assertIn(f"#{r1}", dry_out)
        all_before = json.loads(self.run_cli("status", "--json")[0])["requests"]
        self.assertEqual(len(all_before), 4)

        # --keep preserves newest closed rows; --yes applies deletion only to eligible closed rows
        keep_dry, _ = self.run_cli("prune", "--older-than", "1d", "--keep", "1", "--json")
        keep_data = json.loads(keep_dry)
        self.assertTrue(keep_data["dry_run"])
        self.assertEqual(keep_data["request_ids"], [r1])

        # Record max event ID before pruning r1
        max_ev_before = max(
            json.loads(line)["id"]
            for line in self.run_cli("events")[0].splitlines()
        )

        # Delete r1 and r2 (older than 5d, keep 0), never deleting active r3/r4
        del_out, _ = self.run_cli("prune", "--older-than", "5d", "--yes", "--json")
        del_data = json.loads(del_out)
        self.assertFalse(del_data["dry_run"])
        self.assertEqual(del_data["request_ids"], [r1, r2])
        self.assertEqual(del_data["deleted_requests"], 2)
        self.assertGreater(del_data["deleted_events"], 0)

        remaining = json.loads(self.run_cli("status", "--json")[0])["requests"]
        self.assertEqual([r["id"] for r in remaining], [r3, r4])

        # New registration must produce an event ID strictly greater than max_ev_before
        r5 = self.register(pr=105, days=0, receipt="after-prune")
        new_evs = [
            json.loads(line)
            for line in self.run_cli("events", "--request", str(r5))[0].splitlines()
        ]
        self.assertTrue(all(ev["id"] > max_ev_before for ev in new_evs))

    def test_poller_lock_inspection_live_and_stopped(self):
        connect(self.state).close()
        self.assertEqual(inspect_poller(self.state), {"status": "stopped", "pid": None})

        proc = subprocess.Popen(
            [sys.executable, "-m", "cubrid_ci_watch", "--state-dir", str(self.state), "poll", "--interval", "5"],
            env=self.env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            deadline = time.monotonic() + 5
            seen = None
            while time.monotonic() < deadline:
                seen = inspect_poller(self.state)
                if seen["status"] == "running":
                    break
                time.sleep(0.05)
            self.assertEqual(seen, {"status": "running", "pid": proc.pid})
            out, _ = self.run_cli("status")
            self.assertIn(f"poller: running (pid {proc.pid})", out)
        finally:
            proc.terminate()
            proc.communicate(timeout=5)

        self.assertEqual(inspect_poller(self.state), {"status": "stopped", "pid": None})

    def test_color_and_snapshot_resolution(self):
        fake_tty = io.StringIO()
        fake_tty.isatty = lambda: True

        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertTrue(resolve_color(no_color_flag=False, stream=fake_tty))
            self.assertFalse(resolve_color(no_color_flag=True, stream=fake_tty))

        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            self.assertFalse(resolve_color(no_color_flag=False, stream=fake_tty))

        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1", "NO_COLOR": "1"}, clear=True):
            self.assertTrue(resolve_color(no_color_flag=False, stream=fake_tty))
            self.assertFalse(resolve_color(no_color_flag=True, stream=fake_tty))

        # Snapshot resolution precedence
        with mock.patch.dict(os.environ, {"CUBRID_CI_WATCH_SNAPSHOT": "/tmp/custom-snap"}, clear=True):
            self.assertEqual(resolve_snapshot_executable(), "/tmp/custom-snap")

        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("cubrid_ci_watch.collector.shutil.which", return_value="/usr/local/bin/cubrid-pr-status"):
                self.assertEqual(resolve_snapshot_executable(), "/usr/local/bin/cubrid-pr-status")

            with mock.patch("cubrid_ci_watch.collector.shutil.which", return_value=None):
                with mock.patch("cubrid_ci_watch.collector.Path.home", return_value=self.root):
                    with self.assertRaises(ValueError):
                        resolve_snapshot_executable()


if __name__ == "__main__":
    unittest.main()
