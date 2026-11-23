#!/usr/bin/env python3
"""Tests for per-PR exponential backoff polling and systemd user service management."""

import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC_DIR))

from cubrid_ci_watch import collector, model, service, store  # noqa: E402

HEAD = "a" * 40


class BackoffTest(unittest.TestCase):
    def test_compute_poll_interval_boundaries_and_override(self):
        triggered = 1000.0
        cases = [
            (-5.0, 60),
            (0.0, 60),
            (59.9, 60),
            (60.0, 120),
            (179.9, 120),
            (180.0, 240),
            (419.9, 240),
            (420.0, 480),
            (899.9, 480),
            (900.0, 960),
            (3600.0, 960),
            (172000.0, 960),
        ]
        for elapsed, expected in cases:
            with self.subTest(elapsed=elapsed):
                self.assertEqual(
                    model.compute_poll_interval(triggered, triggered + elapsed, None),
                    expected,
                )
        self.assertEqual(model.compute_poll_interval(triggered, triggered + 10.0, 300), 300)

    def test_poll_one_progression_and_48h_deadline_clamp(self):
        with tempfile.TemporaryDirectory() as tmp:
            state = Path(tmp) / "state"
            db = store.connect(state)
            self.addCleanup(db.close)
            triggered = 1_700_000_000.0
            triggered_iso = model.iso(triggered)
            with mock.patch.object(store, "now", return_value=triggered):
                rid = store.register_request(
                    db,
                    pr=12,
                    head=HEAD,
                    suites=["sql"],
                    triggered_at=triggered_iso,
                    receipt="backoff-test",
                    kind="run",
                    run_id=123,
                    attempt=1,
                )

            pending_obs = {
                "contexts": {
                    "build (debug)": {"state": "PENDING", "attributed": False, "reason": "publisher evidence missing"},
                    "build (release)": {"state": "PENDING", "attributed": False, "reason": "publisher evidence missing"},
                    "test_sql": {"state": "PENDING", "attributed": False, "reason": "publisher evidence missing"},
                }
            }
            expected_deltas = [60, 120, 240, 480, 960, 960]
            current = triggered
            for expected_step in expected_deltas:
                row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
                with (
                    mock.patch.object(collector, "collect", return_value=pending_obs),
                    mock.patch.object(collector, "now", return_value=current),
                ):
                    err = collector.poll_one(db, row, None)
                self.assertFalse(err)
                updated = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
                self.assertEqual(updated["phase"], "active")
                self.assertEqual(updated["next_poll"], current + expected_step)
                current = updated["next_poll"]

            # Near 48h deadline, next_poll clamps to triggered + DEADLINE_SECONDS
            near_deadline = triggered + model.DEADLINE_SECONDS - 100
            row = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            with (
                mock.patch.object(collector, "collect", return_value=pending_obs),
                mock.patch.object(collector, "now", return_value=near_deadline),
            ):
                collector.poll_one(db, row, None)
            clamped = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            self.assertEqual(clamped["next_poll"], triggered + model.DEADLINE_SECONDS)
            self.assertEqual(clamped["phase"], "active")

            # At 48h deadline, request closes with outcome='deadline'
            at_deadline = triggered + model.DEADLINE_SECONDS
            with (
                mock.patch.object(collector, "collect", return_value=pending_obs),
                mock.patch.object(collector, "now", return_value=at_deadline),
            ):
                collector.poll_one(db, clamped, None)
            closed = db.execute("SELECT * FROM requests WHERE id=?", (rid,)).fetchone()
            self.assertEqual(closed["phase"], "closed")
            self.assertEqual(closed["outcome"], "deadline")

    def test_cli_wakeup_validation_and_default_backoff_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state = root / "state"
            fixture = root / "fixture.json"
            fake = root / "fake"
            fake.write_text(
                """#!/usr/bin/env python3
import json, os, sys
data = json.load(open(os.environ['FIXTURE']))
if sys.argv[0].endswith('snapshot'): print(json.dumps(data['snapshot']))
elif '/jobs?' in sys.argv[-1]: print(json.dumps(data['jobs']))
else: print(json.dumps(data['run']))
"""
            )
            fake.chmod(0o755)
            (root / "snapshot").symlink_to(fake)
            (root / "gh").symlink_to(fake)
            when = dt.datetime.now(dt.timezone.utc).replace(microsecond=0)
            fixture.write_text(
                json.dumps(
                    {
                        "snapshot": {
                            "schema_version": 1,
                            "repository": "CUBRID/cubrid",
                            "complete": True,
                            "errors": [],
                            "pr": {"number": 12, "url": "https://github.com/CUBRID/cubrid/pull/12", "head_sha": HEAD},
                            "checks": [],
                        },
                        "run": {
                            "id": 123,
                            "run_attempt": 1,
                            "path": ".github/workflows/gha-ci.yml",
                            "created_at": (when - dt.timedelta(seconds=20)).isoformat(),
                        },
                        "jobs": [{"jobs": []}],
                    }
                )
            )
            env = dict(
                os.environ,
                PYTHONPATH=str(SRC_DIR),
                FIXTURE=str(fixture),
                CUBRID_CI_WATCH_SNAPSHOT=str(root / "snapshot"),
                CUBRID_CI_WATCH_GH=str(root / "gh"),
            )
            bad = subprocess.run(
                [sys.executable, "-m", "cubrid_ci_watch", "--state-dir", str(state), "poll", "--wakeup", "0"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(bad.returncode, 2)

            reg = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "cubrid_ci_watch",
                    "--state-dir",
                    str(state),
                    "register",
                    "12",
                    "--head",
                    HEAD,
                    "--suite",
                    "sql",
                    "--triggered-at",
                    when.isoformat(),
                    "--receipt",
                    "r1",
                ],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(reg.returncode, 0, reg.stderr)

            poll = subprocess.run(
                [sys.executable, "-m", "cubrid_ci_watch", "--state-dir", str(state), "poll", "--once"],
                env=env,
                text=True,
                capture_output=True,
            )
            self.assertEqual(poll.returncode, 0, poll.stderr)
            st = json.loads(
                subprocess.run(
                    [sys.executable, "-m", "cubrid_ci_watch", "--state-dir", str(state), "status", "--json"],
                    env=env,
                    text=True,
                    capture_output=True,
                    check=True,
                ).stdout
            )
            req = st["requests"][0]
            delta = model.timestamp(req["next_poll"]) - model.timestamp(req["triggered_at"])
            self.assertGreaterEqual(delta, 55)
            self.assertLessEqual(delta, 75)


class ServiceTest(unittest.TestCase):
    def test_render_unit_and_setup_idempotence_and_ownership(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env_patch = mock.patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(root / "config"),
                    "CUBRID_CI_WATCH_SERVICE_NAME": "cubrid-ci-watch-test.service",
                },
            )
            runtime = {
                "cli": "/opt/bin/cubrid-ci-watch",
                "snapshot": "/home/u/my-cubrid/bin/cubrid-pr-status",
                "gh": "/usr/bin/gh",
                "gh_pr_info": "/home/u/.local/bin/gh-pr-info",
                "path": "/opt/bin:/home/u/my-cubrid/bin:/usr/bin:/home/u/.local/bin",
            }
            calls = []

            def fake_run(argv, *, check=True):
                calls.append(argv)
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

            with (
                env_patch,
                mock.patch.object(service, "resolve_runtime", return_value=runtime),
                mock.patch.object(service, "_require_linux_systemd"),
                mock.patch.object(service, "run_cmd", side_effect=fake_run),
                mock.patch("sys.stdout", new_callable=io.StringIO),
            ):
                target = service.setup_service(state_dir=root / "state", wakeup=10)
                self.assertTrue(target.exists())
                content = target.read_text()
                self.assertTrue(content.startswith(service.OWNERSHIP_MARKER))
                self.assertIn('"--wakeup" "10"', content)
                self.assertIn("CUBRID_CI_WATCH_SNAPSHOT=", content)
                self.assertIn("CUBRID_CI_WATCH_GH=", content)
                self.assertNotIn("--interval", content)
                self.assertFalse(any("start" in c for c in calls))
                self.assertTrue(any(c[:3] == ["systemd-analyze", "--user", "verify"] for c in calls))
                self.assertTrue(any(c == ["systemctl", "--user", "daemon-reload"] for c in calls))

                # Idempotent repeat setup
                service.setup_service(state_dir=root / "state", wakeup=10)
                self.assertEqual(target.read_text(), content)

                # Refuses foreign unit
                target.write_text("[Unit]\nDescription=Foreign\n")
                with self.assertRaisesRegex(ValueError, "foreign service unit"):
                    service.setup_service(state_dir=root / "state", wakeup=10)

                # Refuses symlink
                target.unlink()
                other = root / "other"
                other.write_text(service.OWNERSHIP_MARKER)
                target.symlink_to(other)
                with self.assertRaisesRegex(ValueError, "refusing symlink"):
                    service.setup_service(state_dir=root / "state", wakeup=10)

    def test_status_is_readonly_when_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with (
                mock.patch.dict(
                    os.environ,
                    {
                        "XDG_CONFIG_HOME": str(root / "config"),
                        "CUBRID_CI_WATCH_SERVICE_NAME": "cubrid-ci-watch-test.service",
                    },
                ),
                mock.patch("sys.stdout", new_callable=io.StringIO) as out,
            ):
                code = service.show_status(state_dir=root / "state")
            self.assertEqual(code, 0)
            self.assertIn("service: not configured", out.getvalue())
            self.assertIn("poller: stopped", out.getvalue())


if __name__ == "__main__":
    unittest.main()
