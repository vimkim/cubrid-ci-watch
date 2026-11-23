"""GitHub Actions status collection, publisher attribution, polling, and wait loop."""

import fcntl
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time

from .model import (
    BASE,
    CONTEXTS,
    DELAY_SECONDS,
    DEADLINE_SECONDS,
    FAILURES,
    MEANINGFUL_EVENTS,
    NO_VERDICT,
    REPO,
    TERMINAL,
    TERMINAL_EVENTS,
    encode,
    hydrate_event,
    now,
    timestamp,
)
from .store import checkpoint_wal, insert_event, query_events


def resolve_snapshot_executable() -> str:
    """Resolve the cubrid-pr-status executable via override, PATH, or legacy my-cubrid/bin."""
    override = os.environ.get("CUBRID_CI_WATCH_SNAPSHOT")
    if override:
        return override
    found = shutil.which("cubrid-pr-status")
    if found:
        return found
    fallback = Path.home() / "my-cubrid/bin/cubrid-pr-status"
    if fallback.is_file() and os.access(fallback, os.X_OK):
        return str(fallback)
    raise ValueError(
        "cubrid-pr-status not found; put it on PATH or set CUBRID_CI_WATCH_SNAPSHOT"
    )


def command_json(argv: list[str]) -> object:
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=120, check=False
        )
        if p.returncode:
            raise ValueError(f"{argv[0]} exited {p.returncode}: {p.stderr.strip()[:300]}")
        return json.loads(p.stdout)
    except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError) as exc:
        raise ValueError(f"{argv[0]}: {exc}") from exc


def get_run(run_id: int) -> dict:
    gh = os.environ.get("CUBRID_CI_WATCH_GH", "gh")
    data = command_json([gh, "api", f"repos/{REPO}/actions/runs/{run_id}"])
    if (
        not isinstance(data, dict)
        or type(data.get("id")) is not int
        or type(data.get("run_attempt")) is not int
        or data["id"] != run_id
        or data["run_attempt"] < 1
        or not isinstance(data.get("path"), str)
        or data["path"].split("@")[0] != ".github/workflows/gha-ci.yml"
    ):
        raise ValueError("invalid run metadata")
    try:
        created = timestamp(data["created_at"])
    except (KeyError, ValueError, TypeError, OverflowError) as exc:
        raise ValueError("invalid run creation time") from exc
    if created > now():
        raise ValueError("run creation time is in the future")
    return data


def published_by(
    job: dict,
    current: dict,
    created: float,
    triggered: float,
    run_id: int,
    current_now: float,
) -> bool:
    try:
        report, start, end = (
            timestamp(current["reported_at"]),
            timestamp(job["started_at"]),
            timestamp(job["completed_at"]),
        )
    except (KeyError, ValueError, TypeError):
        return False
    return (
        job.get("status") == "completed"
        and job.get("conclusion") == "success"
        and created <= start <= report <= end <= current_now
        and report >= triggered
        and current.get("detail_url") == f"{BASE}/actions/runs/{run_id}"
    )


def collect(row) -> dict:
    target = f"{BASE}/pull/{row['pr']}"
    executable = resolve_snapshot_executable()
    snap = command_json([executable, "--json", "--history", "0", target])
    if (
        not isinstance(snap, dict)
        or snap.get("schema_version") != 1
        or snap.get("repository") != REPO
        or snap.get("complete") is not True
        or snap.get("errors") != []
    ):
        raise ValueError("invalid or incomplete snapshot")
    pr = snap.get("pr")
    if (
        not isinstance(pr, dict)
        or pr.get("number") != row["pr"]
        or pr.get("url") != target
        or not isinstance(pr.get("head_sha"), str)
        or not re.fullmatch(r"[0-9a-fA-F]{40}", pr["head_sha"])
    ):
        raise ValueError("invalid PR identity in snapshot")
    if pr["head_sha"].lower() != row["head"]:
        return {"close": "head_changed", "head": pr["head_sha"]}
    checks = snap.get("checks")
    if not isinstance(checks, list):
        raise ValueError("invalid checks in snapshot")
    selected = ["build (debug)", "build (release)"] + [
        "test_" + s for s in json.loads(row["suites"])
    ]
    found = {}
    for check in checks:
        if (
            not isinstance(check, dict)
            or not isinstance(check.get("name"), str)
            or "current" not in check
        ):
            raise ValueError("invalid check row")
        name = check["name"]
        if name not in ["gha-ci: " + c for c in selected]:
            continue
        name = name[len("gha-ci: ") :]
        if name in found:
            raise ValueError("duplicate selected check")
        current = check["current"]
        if current is not None:
            if (
                not isinstance(current, dict)
                or not isinstance(current.get("state"), str)
                or not isinstance(current.get("reported_for_sha"), str)
                or current["reported_for_sha"].lower() != row["head"]
                or any(
                    current.get(k) is not None and not isinstance(current[k], str)
                    for k in ("detail_url", "reported_at")
                )
            ):
                raise ValueError("invalid selected check result")
        found[name] = current

    candidates = set()
    for current in found.values():
        if current and current.get("detail_url"):
            match = re.fullmatch(
                re.escape(BASE) + r"/actions/runs/(\d+)(?:/.*)?",
                current["detail_url"],
            )
            if match:
                candidates.add(int(match.group(1)))

    observations = {
        name: {
            "state": found.get(name, {}).get("state") if found.get(name) else None,
            "attributed": False,
            "reason": "unbound"
            if row["run_id"] is None
            else "publisher evidence missing",
        }
        for name in selected
    }
    if row["run_id"] is None:
        return {"contexts": observations, "candidates": sorted(candidates)}

    run = get_run(row["run_id"])
    if run["run_attempt"] != row["attempt"]:
        if run["run_attempt"] > row["attempt"]:
            return {
                "close": "superseded",
                "run_id": row["run_id"],
                "attempt": run["run_attempt"],
            }
        raise ValueError("bound attempt is not available yet")

    created = timestamp(run["created_at"])
    for candidate in sorted(candidates - {row["run_id"]}):
        replacement = get_run(candidate)
        if timestamp(replacement["created_at"]) > created:
            return {
                "close": "superseded",
                "run_id": candidate,
                "attempt": replacement["run_attempt"],
                "created_at": replacement["created_at"],
                "replaces_run_id": row["run_id"],
            }

    if row["kind"] == "run" and created < row["triggered"]:
        raise ValueError("bound run was created before trigger")

    gh = os.environ.get("CUBRID_CI_WATCH_GH", "gh")
    pages = command_json(
        [
            gh,
            "api",
            "--paginate",
            "--slurp",
            f"repos/{REPO}/actions/runs/{row['run_id']}/attempts/{row['attempt']}/jobs?per_page=100",
        ]
    )
    if not isinstance(pages, list) or any(
        not isinstance(page, dict) or not isinstance(page.get("jobs"), list)
        for page in pages
    ):
        raise ValueError("invalid attempt jobs metadata")
    jobs = [job for page in pages for job in page["jobs"]]
    if any(
        not isinstance(j, dict)
        or not isinstance(j.get("name"), str)
        or any(
            j.get(k) is not None and not isinstance(j[k], str)
            for k in ("started_at", "completed_at")
        )
        for j in jobs
    ):
        raise ValueError("invalid job metadata")
    if get_run(row["run_id"])["run_attempt"] != row["attempt"]:
        raise ValueError("run attempt changed during collection")

    for name in selected:
        current = found.get(name)
        if not current:
            continue
        current_now = now()
        matches = [j for j in jobs if j["name"] in CONTEXTS[name]]
        if len(matches) > 1:
            continue
        if len(matches) == 1:
            if published_by(
                matches[0], current, created, row["triggered"], row["run_id"], current_now
            ):
                observations[name]["attributed"] = True
                observations[name]["reason"] = None
            continue
        if name.startswith("test_") and current.get("state") == "ERROR":
            cancel_matches = [j for j in jobs if j["name"] == "status (cancelled)"]
            if len(cancel_matches) == 1 and published_by(
                cancel_matches[0],
                current,
                created,
                row["triggered"],
                row["run_id"],
                current_now,
            ):
                observations[name]["attributed"] = True
                observations[name]["reason"] = None
    return {"contexts": observations}


def poll_one(db, row, interval: int) -> bool:
    try:
        observation = collect(row)
        error = None
    except (ValueError, TypeError, KeyError, OverflowError) as exc:
        observation, error = None, str(exc)

    current = now()
    db.execute("BEGIN IMMEDIATE")
    with db:
        fresh = db.execute("SELECT * FROM requests WHERE id=?", (row["id"],)).fetchone()
        if fresh["revision"] != row["revision"] or fresh["phase"] != "active":
            return False
        next_poll = (
            min(current + interval, row["triggered"] + DEADLINE_SECONDS)
            if current < row["triggered"] + DEADLINE_SECONDS
            else current + interval
        )
        db.execute(
            "UPDATE requests SET next_poll=?, last_error=? WHERE id=?",
            (next_poll, error, row["id"]),
        )
        if observation is not None:
            if "close" not in observation:
                db.execute(
                    "UPDATE requests SET observation=? WHERE id=?",
                    (encode(observation), row["id"]),
                )
            for candidate in observation.get("candidates", []):
                if candidate != row["run_id"]:
                    insert_event(
                        db,
                        row["id"],
                        f"pickup_candidate:{candidate}",
                        "pickup_candidate",
                        current,
                        {"run_id": candidate},
                    )
            if "close" in observation:
                insert_event(
                    db,
                    row["id"],
                    observation["close"],
                    observation["close"],
                    current,
                    observation,
                )
                db.execute(
                    "UPDATE requests SET phase=?, outcome=? WHERE id=?",
                    ("closed", observation["close"], row["id"]),
                )
                return False
            contexts = observation["contexts"]
            for name, result in contexts.items():
                if result["attributed"] and result["state"] in FAILURES:
                    insert_event(
                        db,
                        row["id"],
                        "failure:" + name,
                        "failure",
                        current,
                        {"context": name, "state": result["state"]},
                    )
                if result["attributed"] and result["state"] in NO_VERDICT:
                    insert_event(
                        db,
                        row["id"],
                        "no_verdict:" + name,
                        "no_verdict",
                        current,
                        {"context": name, "state": result["state"]},
                    )
            if all(r["attributed"] and r["state"] in TERMINAL for r in contexts.values()):
                outcome = (
                    "failure"
                    if any(r["state"] in FAILURES for r in contexts.values())
                    else "no_verdict"
                    if any(r["state"] in NO_VERDICT for r in contexts.values())
                    else "success"
                )
                insert_event(
                    db, row["id"], "complete", "complete", current, {"outcome": outcome}
                )
                db.execute(
                    "UPDATE requests SET phase=?, outcome=? WHERE id=?",
                    ("closed", outcome, row["id"]),
                )
                return False
        if current >= row["triggered"] + DELAY_SECONDS:
            insert_event(db, row["id"], "delay", "delay", current, {})
        if current >= row["triggered"] + DEADLINE_SECONDS:
            insert_event(
                db, row["id"], "deadline", "deadline", current, {"last_error": error}
            )
            db.execute(
                "UPDATE requests SET phase=?, outcome=? WHERE id=?",
                ("closed", "deadline", row["id"]),
            )
    return error is not None


def poll_loop(
    db,
    state_dir: Path,
    *,
    once: bool,
    force: bool,
    interval: int,
    on_events=None,
) -> int:
    if interval <= 0 or (force and not once):
        raise ValueError("--interval must be positive; --force requires --once")
    with open(state_dir / "poll.lock", "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("another poller owns the state directory")
        cursor = db.execute("SELECT COALESCE(MAX(id),0) FROM events").fetchone()[0]
        while True:
            rows = db.execute(
                "SELECT * FROM requests WHERE phase=? AND (? OR next_poll<=?) ORDER BY id",
                ("active", force, now()),
            ).fetchall()
            errors = any([poll_one(db, row, interval) for row in rows])
            checkpoint_wal(db, "PASSIVE")
            new_events = query_events(db, after=cursor)
            if new_events:
                cursor = new_events[-1]["id"]
                if on_events is not None:
                    on_events(new_events)
            if once:
                return 2 if errors else 0
            time.sleep(min(interval, 10))


def wait_for_events(
    db,
    state_dir: Path,
    *,
    request_id: int,
    after: int,
    until: str,
    interval: int,
    timeout: int,
) -> tuple[int, list[dict], str | None]:
    if request_id <= 0 or after < 0 or interval <= 0 or timeout < 0:
        raise ValueError(
            "wait requires positive request_id and --interval, and nonnegative --after and --timeout"
        )
    if not db.execute("SELECT 1 FROM requests WHERE id=?", (request_id,)).fetchone():
        raise ValueError(f"unknown request {request_id}")

    wanted = MEANINGFUL_EVENTS if until == "meaningful" else TERMINAL_EVENTS
    deadline = time.monotonic() + timeout if timeout > 0 else None
    while True:
        rows = db.execute(
            "SELECT * FROM events WHERE request_id=? AND id>? ORDER BY id",
            (request_id, after),
        ).fetchall()
        matched = [hydrate_event(r) for r in rows if r["kind"] in wanted]
        if matched:
            return 0, matched, None
        req = db.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if req["phase"] != "active":
            return (
                0,
                [],
                f"cubrid-ci-watch: request #{request_id} is already closed (outcome={req['outcome']})",
            )
        if deadline is not None and time.monotonic() >= deadline:
            return (
                3,
                [],
                f"cubrid-ci-watch: timed out waiting for request #{request_id}",
            )
        if req["next_poll"] <= now():
            with open(state_dir / "poll.lock", "a") as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    fresh = db.execute(
                        "SELECT * FROM requests WHERE id=?", (request_id,)
                    ).fetchone()
                    if fresh and fresh["phase"] == "active" and fresh["next_poll"] <= now():
                        poll_one(db, fresh, interval)
                        checkpoint_wal(db, "PASSIVE")
                        continue
        sleep_for = min(interval, 5)
        if deadline is not None:
            sleep_for = min(sleep_for, max(0.0, deadline - time.monotonic()))
        if sleep_for > 0:
            time.sleep(sleep_for)
