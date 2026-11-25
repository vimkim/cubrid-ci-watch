#!/usr/bin/env python3
"""Tests for required-only completion, wait --until required, register --required-only, and drop-suite."""

import datetime as dt
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

SRC_DIR = Path(__file__).resolve().parents[1] / "src"
HEAD = "a" * 40
BASE = "https://github.com/CUBRID/cubrid"


class RequiredOnlyTest(unittest.TestCase):
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
    print(json.dumps(data['snapshot']))
elif sys.argv[1:3] == ['pr', 'checks']:
    rc = data.get('checks_exit', 0)
    if data.get('checks_stdout') is not None:
        print(data['checks_stdout'])
    else:
        print(json.dumps(data.get('required_checks', [])))
    if rc:
        sys.stderr.write(data.get('checks_stderr', 'checks failed\\n'))
    sys.exit(rc)
elif '/jobs?' in sys.argv[-1]:
    print(json.dumps(data['jobs']))
else:
    print(json.dumps(data.get('runs', {}).get(sys.argv[-1].split('/')[-1], data['run'])))
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

    def cmd(self, *args):
        return [
            sys.executable,
            "-m",
            "cubrid_ci_watch",
            "--state-dir",
            str(self.state),
            *args,
        ]

    def run_cli(self, *args, expected=0):
        proc = subprocess.run(
            self.cmd(*args),
            env=self.env,
            text=True,
            capture_output=True,
            timeout=15,
        )
        self.assertEqual(proc.returncode, expected, proc.stderr)
        return proc.stdout, proc.stderr

    def write_fixture(
        self,
        states=None,
        *,
        required_checks=None,
        checks_exit=0,
        checks_stdout=None,
    ):
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
            for name in (
                "build status",
                "shell / status shell",
                "sql / status sql",
                "medium / status medium",
                "sql_by_cci / status sql_by_cci",
            )
        ]
        if required_checks is None:
            required_checks = [
                {"name": "license"},
                {"name": "gha-ci: build (debug)"},
                {"name": "gha-ci: build (release)"},
                {"name": "gha-ci: test_shell"},
                {"name": "gha-ci: test_sql"},
                {"name": "gha-ci: test_medium"},
            ]
        self.fixture.write_text(
            json.dumps(
                {
                    "snapshot": {
                        "schema_version": 1,
                        "repository": "CUBRID/cubrid",
                        "complete": True,
                        "errors": [],
                        "pr": {"number": 8734, "url": BASE + "/pull/8734", "head_sha": HEAD},
                        "checks": checks,
                    },
                    "run": {
                        "id": 123,
                        "run_attempt": 1,
                        "path": ".github/workflows/gha-ci.yml",
                        "created_at": (self.when - dt.timedelta(minutes=9)).isoformat(),
                    },
                    "jobs": [{"jobs": jobs}],
                    "required_checks": required_checks,
                    "checks_exit": checks_exit,
                    "checks_stdout": checks_stdout,
                }
            )
        )

    def register(self, *suites, receipt="r1", binding=True, required_only=False):
        args = [
            "register",
            "8734",
            "--head",
            HEAD,
            "--triggered-at",
            (self.when - dt.timedelta(minutes=10)).isoformat(),
            "--receipt",
            receipt,
        ]
        for s in suites:
            args += ["--suite", s]
        if required_only:
            args.append("--required-only")
        if binding:
            args += ["--run-id", "123", "--attempt", "1"]
        out, _ = self.run_cli(*args)
        return json.loads(out)["request_id"]

    def events(self, rid):
        out, _ = self.run_cli("events", "--request", str(rid))
        return [json.loads(line) for line in out.splitlines() if line.strip()]

    def show_json(self, rid):
        out, _ = self.run_cli("show", str(rid), "--json")
        return json.loads(out)

    def test_incident_required_completes_while_optional_sql_by_cci_pending(self):
        rid = self.register("shell", "sql", "medium", "sql_by_cci", receipt="inc-95")
        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_shell": "SUCCESS",
                "test_sql": "SUCCESS",
                "test_medium": "SUCCESS",
                "test_sql_by_cci": "PENDING",
            }
        )
        streamed = [
            json.loads(line)
            for line in self.run_cli("poll", "--once")[0].splitlines()
            if line.strip()
        ]
        self.assertEqual([e["kind"] for e in streamed], ["required_complete"])
        ev = streamed[0]
        self.assertEqual(ev["payload"]["outcome"], "success")
        self.assertEqual(ev["payload"]["required_source"], "live+stable")
        self.assertEqual(
            ev["payload"]["required_contexts"],
            ["build (debug)", "build (release)", "test_medium", "test_shell", "test_sql"],
        )

        # Request stays active so sql_by_cci can still be observed later, and is deduplicated.
        self.run_cli("poll", "--once", "--force")
        evs = self.events(rid)
        self.assertEqual(sum(e["kind"] == "required_complete" for e in evs), 1)
        data = self.show_json(rid)
        self.assertEqual(data["request"]["phase"], "active")
        contexts = data["request"]["observation"]["contexts"]
        self.assertFalse(contexts["test_sql_by_cci"]["required"])
        self.assertTrue(contexts["test_shell"]["required"])

        # wait --until required wakes on required_complete with exit 0.
        out, _ = self.run_cli("wait", str(rid), "--after", "0", "--until", "required")
        waited = [json.loads(line) for line in out.splitlines()]
        self.assertEqual([e["kind"] for e in waited], ["required_complete"])

        # Calling wait --until required after required_complete exits 0 immediately without hanging.
        out2, err2 = self.run_cli(
            "wait",
            str(rid),
            "--after",
            str(ev["id"]),
            "--until",
            "required",
            "--interval",
            "10",
            "--timeout",
            "2",
        )
        self.assertEqual(out2, "")
        self.assertIn("required contexts are already complete", err2)

        # Human `show` marks optional context clearly.
        human_show, _ = self.run_cli("show", str(rid))
        self.assertIn("test_sql_by_cci", human_show)
        self.assertIn(", optional)", human_show)

    def test_optional_failure_does_not_wake_required_wait_and_preserves_required_outcome(self):
        rid = self.register("shell", "sql_by_cci", receipt="opt-fail")
        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_shell": "PENDING",
                "test_sql_by_cci": "FAILURE",
            }
        )
        self.run_cli("poll", "--once")
        evs = self.events(rid)
        failure_evs = [e for e in evs if e["kind"] == "failure"]
        self.assertEqual(len(failure_evs), 1)
        self.assertFalse(failure_evs[0]["payload"]["required"])

        # wait --until required must NOT wake on optional sql_by_cci failure.
        _, err = self.run_cli(
            "wait",
            str(rid),
            "--after",
            "0",
            "--until",
            "required",
            "--interval",
            "10",
            "--timeout",
            "1",
            expected=3,
        )
        self.assertIn("timed out", err)

        # Once required contexts finish, request closes with overall failure & required_outcome=success.
        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_shell": "SUCCESS",
                "test_sql_by_cci": "FAILURE",
            }
        )
        self.run_cli("poll", "--once", "--force")
        req = self.show_json(rid)["request"]
        self.assertEqual(req["phase"], "closed")
        self.assertEqual(req["outcome"], "failure")
        complete_ev = self.events(rid)[-1]
        self.assertEqual(complete_ev["kind"], "complete")
        self.assertEqual(complete_ev["payload"]["outcome"], "failure")
        self.assertEqual(complete_ev["payload"]["required_outcome"], "success")

        out, _ = self.run_cli("wait", str(rid), "--after", "0", "--until", "required")
        self.assertEqual([json.loads(line)["kind"] for line in out.splitlines()], ["complete"])

    def test_required_failure_with_pending_optional_emits_required_complete_failure(self):
        rid = self.register("shell", "sql", "sql_by_cci", receipt="req-fail")
        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_sql": "FAILURE",
                "test_shell": "PENDING",
                "test_sql_by_cci": "PENDING",
            }
        )
        self.run_cli("poll", "--once")
        evs = self.events(rid)
        fail_ev = next(e for e in evs if e["kind"] == "failure")
        self.assertTrue(fail_ev["payload"]["required"])
        self.assertNotIn("required_complete", [e["kind"] for e in evs])

        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_sql": "FAILURE",
                "test_shell": "SUCCESS",
                "test_sql_by_cci": "PENDING",
            }
        )
        self.run_cli("poll", "--once", "--force")
        evs2 = self.events(rid)
        req_ev = next(e for e in evs2 if e["kind"] == "required_complete")
        self.assertEqual(req_ev["payload"]["outcome"], "failure")
        self.assertEqual(self.show_json(rid)["request"]["phase"], "active")

    def test_stable_fallback_and_cached_live_required_and_exit_8(self):
        # 1) Exit 1 falls back to stable contract (build (debug) is optional).
        rid = self.register("shell", "sql_by_cci", receipt="fallback")
        self.write_fixture(
            {
                "build (debug)": "PENDING",
                "build (release)": "SUCCESS",
                "test_shell": "SUCCESS",
                "test_sql_by_cci": "PENDING",
            },
            checks_exit=1,
        )
        self.run_cli("poll", "--once")
        req = self.show_json(rid)["request"]
        self.assertEqual(req["observation"]["required_source"], "stable")
        self.assertFalse(req["observation"]["contexts"]["build (debug)"]["required"])
        self.assertIn("required_complete", [e["kind"] for e in self.events(rid)])

        # 2) Poll 1 succeeds (exit 8 allowed) with build (debug) live-required; Poll 2 errors -> cached+stable.
        rid2 = self.register("shell", "sql_by_cci", receipt="cached")
        self.write_fixture(
            {
                "build (debug)": "PENDING",
                "build (release)": "SUCCESS",
                "test_shell": "PENDING",
                "test_sql_by_cci": "PENDING",
            },
            checks_exit=8,
        )
        self.run_cli("poll", "--once")
        req2 = self.show_json(rid2)["request"]
        self.assertEqual(req2["observation"]["required_source"], "live+stable")
        self.assertTrue(req2["observation"]["contexts"]["build (debug)"]["required"])

        self.write_fixture(
            {
                "build (debug)": "PENDING",
                "build (release)": "SUCCESS",
                "test_shell": "SUCCESS",
                "test_sql_by_cci": "PENDING",
            },
            checks_exit=1,
        )
        self.run_cli("poll", "--once", "--force")
        req2_after = self.show_json(rid2)["request"]
        self.assertEqual(req2_after["observation"]["required_source"], "cached+stable")
        self.assertTrue(req2_after["observation"]["contexts"]["build (debug)"]["required"])
        self.assertNotIn("required_complete", [e["kind"] for e in self.events(rid2)])

    def test_register_required_only_and_idempotence(self):
        # Default --required-only without --suite selects medium, shell, sql
        rid1 = self.register(receipt="req-only-default", required_only=True, binding=False)
        self.assertEqual(self.show_json(rid1)["request"]["suites"], ["medium", "shell", "sql"])
        # Idempotent bind with --required-only
        rid1_bind = self.register(receipt="req-only-default", required_only=True, binding=True)
        self.assertEqual(rid1, rid1_bind)

        # Subset + optional filters out sql_by_cci
        rid2 = self.register("shell", "sql_by_cci", receipt="req-only-sub", required_only=True)
        self.assertEqual(self.show_json(rid2)["request"]["suites"], ["shell"])

        # Only optional suite with --required-only fails
        self.register_expect_fail("--suite", "sql_by_cci", "--required-only", receipt="only-opt")
        # Neither --suite nor --required-only fails
        self.register_expect_fail(receipt="empty-suites")

    def register_expect_fail(self, *extra_args, receipt="fail"):
        self.run_cli(
            "register",
            "8734",
            "--head",
            HEAD,
            "--triggered-at",
            self.when.isoformat(),
            "--receipt",
            receipt,
            *extra_args,
            expected=2,
        )

    def test_drop_suite_and_validation(self):
        rid = self.register("shell", "sql", "medium", "sql_by_cci", receipt="drop-test")
        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_shell": "SUCCESS",
                "test_sql": "SUCCESS",
                "test_medium": "SUCCESS",
                "test_sql_by_cci": "PENDING",
            }
        )
        self.run_cli("poll", "--once")
        self.assertEqual(self.show_json(rid)["request"]["phase"], "active")

        out, _ = self.run_cli("drop-suite", str(rid), "--suite", "sql_by_cci")
        res = json.loads(out)
        self.assertEqual(res["suites"], ["medium", "shell", "sql"])
        self.assertEqual(res["removed"], ["sql_by_cci"])

        # Repeat drop-suite is an idempotent no-op
        out2, _ = self.run_cli("drop-suite", str(rid), "--suite", "sql_by_cci")
        self.assertEqual(json.loads(out2)["removed"], [])

        # Next poll completes now that sql_by_cci was dropped
        self.run_cli("poll", "--once")
        req = self.show_json(rid)["request"]
        self.assertEqual(req["phase"], "closed")
        self.assertEqual(req["outcome"], "success")
        kinds = [e["kind"] for e in self.events(rid)]
        self.assertIn("suites_updated", kinds)
        self.assertIn("complete", kinds)

        # Validation errors: closed request, unknown request, unknown suite, dropping all remaining suites
        self.run_cli("drop-suite", str(rid), "--suite", "sql", expected=2)
        self.run_cli("drop-suite", "99999", "--suite", "sql", expected=2)

        rid_single = self.register("shell", receipt="drop-last")
        self.run_cli("drop-suite", str(rid_single), "--suite", "bogus", expected=2)
        self.run_cli("drop-suite", str(rid_single), "--suite", "shell", expected=2)

    def test_legacy_observation_backward_compatibility(self):
        rid = self.register("shell", "sql_by_cci", receipt="legacy-obs")
        legacy_obs = {
            "contexts": {
                "build (debug)": {"state": "SUCCESS", "attributed": True, "reason": None},
                "build (release)": {"state": "SUCCESS", "attributed": True, "reason": None},
                "test_shell": {"state": "PENDING", "attributed": False, "reason": "publisher evidence missing"},
                "test_sql_by_cci": {"state": "PENDING", "attributed": False, "reason": "publisher evidence missing"},
            }
        }
        db = sqlite3.connect(self.state / "watch.sqlite3")
        with db:
            db.execute(
                "UPDATE requests SET observation=? WHERE id=?",
                (json.dumps(legacy_obs), rid),
            )
        db.close()

        show_out, _ = self.run_cli("show", str(rid))
        self.assertIn("test_shell", show_out)
        status_out, _ = self.run_cli("status", "-v")
        self.assertIn("test_sql_by_cci", status_out)

        self.write_fixture(
            {
                "build (debug)": "SUCCESS",
                "build (release)": "SUCCESS",
                "test_shell": "SUCCESS",
                "test_sql_by_cci": "PENDING",
            }
        )
        self.run_cli("poll", "--once", "--force")
        req = self.show_json(rid)["request"]
        self.assertEqual(req["phase"], "active")
        self.assertFalse(req["observation"]["contexts"]["test_sql_by_cci"]["required"])
        self.assertIn("required_complete", [e["kind"] for e in self.events(rid)])


if __name__ == "__main__":
    unittest.main()
