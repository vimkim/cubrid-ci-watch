"""Domain constants, time/JSON helpers, input parsers, and row hydrators."""

import datetime as dt
import json
import re

REPO = "CUBRID/cubrid"
BASE = "https://github.com/" + REPO

CONTEXTS = {
    "build (debug)": ("build status",),
    "build (release)": ("build status",),
    "test_shell": ("shell / status shell", "status shell"),
    "test_sql": ("sql / status sql", "status sql"),
    "test_medium": ("medium / status medium", "status medium"),
    "test_sql_by_cci": ("sql_by_cci / status sql_by_cci", "status sql_by_cci"),
}

SUITES = ("shell", "sql", "medium", "sql_by_cci")
FAILURES = {"FAILURE"}
NO_VERDICT = {"ERROR", "TIMED_OUT", "CANCELLED", "ACTION_REQUIRED", "STARTUP_FAILURE"}
TERMINAL = FAILURES | NO_VERDICT | {"SUCCESS"}
MEANINGFUL_EVENTS = {
    "pickup_candidate",
    "failure",
    "no_verdict",
    "complete",
    "head_changed",
    "superseded",
    "delay",
    "deadline",
}
TERMINAL_EVENTS = {"complete", "head_changed", "superseded", "deadline"}

DELAY_SECONDS = 86400
DEADLINE_SECONDS = 172800
BACKOFF_STEPS_SECONDS = (60, 120, 240, 480, 960)
DEFAULT_WAKEUP_SECONDS = 10


def compute_poll_interval(
    triggered: float, current: float, interval: int | None
) -> int:
    """Return next poll interval in seconds: fixed if given, else 1/2/4/8/16m backoff."""
    if interval is not None:
        return interval
    elapsed = max(0.0, current - triggered)
    threshold = 0
    for step in BACKOFF_STEPS_SECONDS[:-1]:
        threshold += step
        if elapsed < threshold:
            return step
    return BACKOFF_STEPS_SECONDS[-1]

_DURATION_UNITS = {
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
}


def now() -> float:
    return dt.datetime.now(dt.timezone.utc).timestamp()


def timestamp(value: str) -> float:
    if not isinstance(value, str):
        raise ValueError("timestamp must be a string")
    parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.timestamp()


def iso(value: float) -> str:
    return (
        dt.datetime.fromtimestamp(value, dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
    )


def encode(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def parse_pr_number(value: str) -> int:
    match = re.fullmatch(
        r"(?:https://github\.com/CUBRID/cubrid/pull/)?([1-9][0-9]*)", str(value)
    )
    if not match:
        raise ValueError("use a positive PR number or canonical CUBRID PR URL")
    return int(match.group(1))


def parse_duration(value: str) -> int:
    """Parse a positive duration such as '30d', '12h', '2w', '90m', or '3600s' into seconds."""
    text = str(value).strip().lower()
    match = re.fullmatch(r"([1-9][0-9]*)([smhdw])", text)
    if not match:
        raise ValueError("duration must look like 30d, 12h, 2w, 90m, or 3600s")
    amount, unit = int(match.group(1)), match.group(2)
    return amount * _DURATION_UNITS[unit]


def hydrate_request(row) -> dict:
    """Convert a SQLite requests row into the versioned v1 dict representation."""
    d = dict(row)
    d["suites"] = json.loads(d["suites"])
    d["observation"] = json.loads(d["observation"]) if d["observation"] else None
    d["triggered_at"] = iso(d.pop("triggered"))
    d["deadline_at"] = iso(timestamp(d["triggered_at"]) + DEADLINE_SECONDS)
    d["next_poll"] = iso(d["next_poll"])
    return d


def hydrate_event(row) -> dict:
    """Convert a SQLite events row into the versioned v1 event dict representation."""
    return {
        "schema_version": 1,
        "id": row["id"],
        "request_id": row["request_id"],
        "key": row["key"],
        "kind": row["kind"],
        "occurred_at": iso(row["occurred"]),
        "payload": json.loads(row["payload"]),
    }
