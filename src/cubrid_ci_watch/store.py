"""SQLite ledger persistence, query helpers, pruning, and lock inspection."""

import fcntl
import json
import os
from pathlib import Path
import re
import sqlite3

from .model import (
    OPTIONAL_SUITES,
    REQUIRED_SUITES,
    SUITES,
    encode,
    hydrate_event,
    hydrate_request,
    now,
    timestamp,
)

_WAL_MODES = {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}


def default_state_dir() -> Path:
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local/state"
    return base / "cubrid-ci-watch"


def connect(path: Path) -> sqlite3.Connection:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = sqlite3.connect(path / "watch.sqlite3", timeout=30)
    db.row_factory = sqlite3.Row
    with open(path / "init.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        db.executescript(
            """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS requests (
      id INTEGER PRIMARY KEY, receipt TEXT UNIQUE NOT NULL, pr INTEGER NOT NULL,
      head TEXT NOT NULL, suites TEXT NOT NULL, triggered REAL NOT NULL, kind TEXT NOT NULL,
      run_id INTEGER, attempt INTEGER, revision INTEGER NOT NULL DEFAULT 0,
      phase TEXT NOT NULL DEFAULT 'active', outcome TEXT, next_poll REAL NOT NULL,
      observation TEXT, last_error TEXT);
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT, request_id INTEGER NOT NULL,
      key TEXT NOT NULL, kind TEXT NOT NULL, occurred REAL NOT NULL, payload TEXT NOT NULL,
      UNIQUE(request_id, key));
    """
        )
    return db


def checkpoint_wal(db: sqlite3.Connection, mode: str = "PASSIVE") -> None:
    if mode not in _WAL_MODES:
        raise ValueError(f"unsupported WAL checkpoint mode: {mode}")
    try:
        db.execute(f"PRAGMA wal_checkpoint({mode})")
    except sqlite3.Error:
        pass


def inspect_poller(state_dir: Path) -> dict:
    """Inspect whether an active poller holds poll.lock via Linux /proc/locks."""
    lock_path = state_dir / "poll.lock"
    try:
        st = lock_path.stat()
    except FileNotFoundError:
        return {"status": "stopped", "pid": None}
    except OSError:
        return {"status": "unknown", "pid": None}

    proc_locks = Path("/proc/locks")
    try:
        text = proc_locks.read_text()
    except OSError:
        return {"status": "unknown", "pid": None}

    target = (os.major(st.st_dev), os.minor(st.st_dev), st.st_ino)
    for line in text.splitlines():
        fields = line.split()
        if len(fields) >= 8 and fields[1:4] == ["FLOCK", "ADVISORY", "WRITE"]:
            try:
                major, minor, inode = fields[5].split(":")
                if (int(major, 16), int(minor, 16), int(inode)) == target:
                    return {"status": "running", "pid": int(fields[4])}
            except ValueError:
                continue
    return {"status": "stopped", "pid": None}


def insert_event(
    db: sqlite3.Connection,
    request_id: int,
    key: str,
    kind: str,
    when: float,
    payload: dict,
) -> None:
    db.execute(
        "INSERT OR IGNORE INTO events(request_id,key,kind,occurred,payload) VALUES(?,?,?,?,?)",
        (request_id, key, kind, when, encode(payload)),
    )


def register_request(
    db: sqlite3.Connection,
    *,
    pr: int,
    head: str,
    suites: list[str],
    triggered_at: str,
    receipt: str,
    kind: str,
    run_id: int | None = None,
    attempt: int | None = None,
    required_only: bool = False,
) -> int:
    if pr <= 0 or not re.fullmatch(r"[0-9a-fA-F]{40}", head):
        raise ValueError("PR must be positive and head must be a full 40-character SHA")
    if not receipt.strip():
        raise ValueError("receipt must not be empty")
    normalized_suites = sorted(set(s.removeprefix("test_") for s in suites))
    if any(s not in SUITES for s in normalized_suites):
        raise ValueError("select shell, sql, medium, or sql_by_cci with --suite")
    if required_only:
        if not suites:
            normalized_suites = sorted(REQUIRED_SUITES)
        else:
            normalized_suites = [s for s in normalized_suites if s not in OPTIONAL_SUITES]
            if not normalized_suites:
                raise ValueError("no required suites selected after excluding optional suites")
    elif not normalized_suites:
        raise ValueError("select shell, sql, medium, or sql_by_cci with --suite")
    if (run_id is None) != (attempt is None) or (
        run_id is not None and (run_id <= 0 or attempt <= 0)
    ):
        raise ValueError("--run-id and --attempt must be positive and supplied together")
    if kind == "rerun-failed" and run_id is None:
        raise ValueError("rerun-failed requires --run-id and --attempt")
    triggered = timestamp(triggered_at)
    current_now = now()
    if triggered > current_now + 60:
        raise ValueError("triggered-at must not be in the future")
    facts = (pr, head.lower(), encode(normalized_suites), triggered, kind)

    db.execute("BEGIN IMMEDIATE")
    with db:
        row = db.execute(
            "SELECT * FROM requests WHERE receipt=?", (receipt,)
        ).fetchone()
        if row:
            if tuple(row[k] for k in ("pr", "head", "suites", "triggered", "kind")) != facts:
                raise ValueError("receipt already registered with different request facts")
            if run_id is not None:
                if row["run_id"] is None:
                    if row["phase"] != "active":
                        raise ValueError("cannot bind a closed request")
                    ts = now()
                    db.execute(
                        "UPDATE requests SET run_id=?, attempt=?, revision=revision+1, next_poll=? WHERE id=?",
                        (run_id, attempt, ts, row["id"]),
                    )
                    insert_event(
                        db,
                        row["id"],
                        "binding",
                        "binding",
                        ts,
                        {"run_id": run_id, "attempt": attempt, "source": "caller"},
                    )
                elif (row["run_id"], row["attempt"]) != (run_id, attempt):
                    raise ValueError("receipt already has a different run binding")
            return row["id"]

        ts = now()
        cursor = db.execute(
            "INSERT INTO requests(receipt,pr,head,suites,triggered,kind,run_id,attempt,next_poll) VALUES(?,?,?,?,?,?,?,?,?)",
            (receipt, *facts, run_id, attempt, ts),
        )
        request_id = cursor.lastrowid
        insert_event(
            db, request_id, "registered", "registered", ts, {"receipt": receipt}
        )
        if run_id is not None:
            insert_event(
                db,
                request_id,
                "binding",
                "binding",
                ts,
                {"run_id": run_id, "attempt": attempt, "source": "caller"},
            )
        return request_id


def drop_request_suites(
    db: sqlite3.Connection, request_id: int, suites: list[str]
) -> dict:
    """Remove tracked suite(s) from an active request so they no longer gate completion."""
    if request_id <= 0:
        raise ValueError("request_id must be positive")
    to_remove = sorted(set(s.removeprefix("test_") for s in suites))
    if not to_remove or any(s not in SUITES for s in to_remove):
        raise ValueError("select shell, sql, medium, or sql_by_cci with --suite")

    db.execute("BEGIN IMMEDIATE")
    with db:
        row = db.execute(
            "SELECT * FROM requests WHERE id=?", (request_id,)
        ).fetchone()
        if row is None:
            raise ValueError(f"unknown request {request_id}")
        if row["phase"] != "active":
            raise ValueError("cannot modify a closed request")
        current_suites = json.loads(row["suites"])
        removed = [s for s in to_remove if s in current_suites]
        remaining = [s for s in current_suites if s not in to_remove]
        if not remaining:
            raise ValueError("at least one suite must remain tracked")
        if not removed:
            return {"request_id": request_id, "suites": current_suites, "removed": []}
        ts = now()
        new_revision = row["revision"] + 1
        db.execute(
            "UPDATE requests SET suites=?, revision=?, next_poll=? WHERE id=?",
            (encode(remaining), new_revision, ts, request_id),
        )
        insert_event(
            db,
            request_id,
            f"suites_updated:{new_revision}",
            "suites_updated",
            ts,
            {"removed": removed, "suites": remaining},
        )
        return {"request_id": request_id, "suites": remaining, "removed": removed}


def request_counts(db: sqlite3.Connection) -> dict:
    row = db.execute(
        """
        SELECT
          COUNT(*) AS total,
          COALESCE(SUM(CASE WHEN phase = 'active' THEN 1 ELSE 0 END), 0) AS active,
          COALESCE(SUM(CASE WHEN phase != 'active' THEN 1 ELSE 0 END), 0) AS closed
        FROM requests
        """
    ).fetchone()
    return {
        "total": int(row["total"]),
        "active": int(row["active"]),
        "closed": int(row["closed"]),
    }


def query_requests(
    db: sqlite3.Connection,
    *,
    phase: str | None = None,
    pr: int | None = None,
    outcome: str | None = None,
    order: str = "asc",
    limit: int | None = None,
) -> list[dict]:
    if order not in ("asc", "desc"):
        raise ValueError("order must be 'asc' or 'desc'")
    if limit is not None and limit <= 0:
        raise ValueError("limit must be positive")

    clauses = []
    params: list[object] = []
    if phase is not None:
        clauses.append("phase=?")
        params.append(phase)
    if pr is not None:
        clauses.append("pr=?")
        params.append(pr)
    if outcome is not None:
        clauses.append("outcome=?")
        params.append(outcome)

    sql = "SELECT * FROM requests"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    sql += f" ORDER BY id {'ASC' if order == 'asc' else 'DESC'}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    return [hydrate_request(row) for row in db.execute(sql, params)]


def get_request(db: sqlite3.Connection, request_id: int) -> dict | None:
    row = db.execute("SELECT * FROM requests WHERE id=?", (request_id,)).fetchone()
    return hydrate_request(row) if row else None


def query_events(
    db: sqlite3.Connection,
    *,
    after: int = 0,
    request_id: int | None = None,
    kind: str | None = None,
    limit: int | None = None,
    newest_first: bool = False,
) -> list[dict]:
    if after < 0:
        raise ValueError("--after must be nonnegative")
    if limit is not None and limit <= 0:
        raise ValueError("--limit must be positive")

    clauses = ["id>?"]
    params: list[object] = [after]
    if request_id is not None:
        clauses.append("request_id=?")
        params.append(request_id)
    if kind is not None:
        clauses.append("kind=?")
        params.append(kind)

    direction = "DESC" if newest_first else "ASC"
    sql = f"SELECT * FROM events WHERE {' AND '.join(clauses)} ORDER BY id {direction}"
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)

    rows = [hydrate_event(row) for row in db.execute(sql, params)]
    if newest_first:
        rows.reverse()
    return rows


def count_events_for_request(db: sqlite3.Connection, request_id: int) -> int:
    row = db.execute(
        "SELECT COUNT(*) AS cnt FROM events WHERE request_id=?", (request_id,)
    ).fetchone()
    return int(row["cnt"])


def select_prunable_requests(
    db: sqlite3.Connection,
    *,
    older_than_seconds: int,
    keep: int = 0,
    ref_now: float | None = None,
) -> tuple[list[dict], int]:
    if older_than_seconds <= 0:
        raise ValueError("--older-than must be positive")
    if keep < 0:
        raise ValueError("--keep must be nonnegative")

    cutoff = (now() if ref_now is None else ref_now) - older_than_seconds
    closed_rows = db.execute(
        "SELECT * FROM requests WHERE phase='closed' ORDER BY id DESC"
    ).fetchall()
    eligible_rows = [row for row in closed_rows[keep:] if row["triggered"] <= cutoff]
    if not eligible_rows:
        return [], 0

    ids = [row["id"] for row in eligible_rows]
    placeholders = ",".join("?" for _ in ids)
    event_count = int(
        db.execute(
            f"SELECT COUNT(*) AS cnt FROM events WHERE request_id IN ({placeholders})",
            ids,
        ).fetchone()["cnt"]
    )
    candidates = [hydrate_request(row) for row in reversed(eligible_rows)]
    return candidates, event_count


def prune_requests(
    db: sqlite3.Connection, request_ids: list[int]
) -> tuple[int, int]:
    if not request_ids:
        return 0, 0
    placeholders = ",".join("?" for _ in request_ids)
    db.execute("BEGIN IMMEDIATE")
    with db:
        closed_ids = [
            row["id"]
            for row in db.execute(
                f"SELECT id FROM requests WHERE phase='closed' AND id IN ({placeholders})",
                request_ids,
            )
        ]
        if not closed_ids:
            return 0, 0
        closed_placeholders = ",".join("?" for _ in closed_ids)
        ev_cur = db.execute(
            f"DELETE FROM events WHERE request_id IN ({closed_placeholders})",
            closed_ids,
        )
        req_cur = db.execute(
            f"DELETE FROM requests WHERE id IN ({closed_placeholders})",
            closed_ids,
        )
    checkpoint_wal(db, "TRUNCATE")
    return req_cur.rowcount, ev_cur.rowcount
