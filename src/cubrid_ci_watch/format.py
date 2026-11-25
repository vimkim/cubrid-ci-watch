"""Human-readable and grep-friendly plain formatters."""

import os

from .model import encode, now, timestamp

_SHORT_CONTEXTS = (
    ("build (debug)", "dbg"),
    ("build (release)", "rel"),
    ("test_medium", "med"),
    ("test_shell", "sh"),
    ("test_sql", "sql"),
    ("test_sql_by_cci", "cci"),
)

_SHORT_STATES = {
    "SUCCESS": "OK",
    "FAILURE": "FAIL",
    "PENDING": "PEND",
    "ERROR": "ERR",
    "CANCELLED": "CANC",
    "TIMED_OUT": "TIME",
    "ACTION_REQUIRED": "ACT",
    "STARTUP_FAILURE": "START",
    "NEUTRAL": "NEUT",
    "SKIPPED": "SKIP",
}

_ANSI_RESET = "\x1b[0m"
_ANSI_DIM = "\x1b[2m"
_ANSI_BOLD = "\x1b[1m"
_ANSI_GREEN = "\x1b[32m"
_ANSI_RED = "\x1b[31m"
_ANSI_YELLOW = "\x1b[33m"
_ANSI_CYAN = "\x1b[36m"


def resolve_color(*, no_color_flag: bool, stream) -> bool:
    if no_color_flag:
        return False
    force = os.environ.get("FORCE_COLOR")
    if force is not None:
        return force in ("", "1", "2", "3", "true")
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("TERM") == "dumb":
        return False
    isatty = getattr(stream, "isatty", None)
    return bool(isatty and isatty())


def _paint(text: str, code: str, *, color: bool) -> str:
    if not color or not code:
        return text
    return f"{code}{text}{_ANSI_RESET}"


def relative_time(target_ts: float, ref_ts: float) -> str:
    delta = int(round(target_ts - ref_ts))
    future = delta >= 0
    secs = abs(delta)
    if secs < 60:
        unit = f"{secs}s"
    elif secs < 3600:
        unit = f"{secs // 60}m"
    elif secs < 86400:
        unit = f"{secs // 3600}h"
    else:
        unit = f"{secs // 86400}d"
    return f"in {unit}" if future else f"{unit} ago"


def _deadline_label(req: dict, ref_ts: float) -> str:
    if req["phase"] == "active":
        target = timestamp(req["deadline_at"])
        delta = int(round(target - ref_ts))
        if delta < 0:
            secs = abs(delta)
            if secs < 3600:
                return f"overdue {secs // 60}m"
            return f"overdue {secs // 3600}h"
        return relative_time(target, ref_ts)
    return relative_time(timestamp(req["triggered_at"]), ref_ts)


def _short_state(state: str | None, attributed: bool) -> str:
    if state is None:
        return "-"
    label = _SHORT_STATES.get(state, state[:4])
    return label if attributed else label + "?"


def compact_checks(req: dict, *, color: bool = False) -> str:
    obs = req.get("observation")
    if not obs:
        base = _paint("(no observation)", _ANSI_DIM, color=color)
        return base + (" [err]" if req.get("last_error") else "")

    contexts = obs.get("contexts", {})
    parts = []
    seen = set()
    for full_name, short_name in _SHORT_CONTEXTS:
        if full_name not in contexts:
            continue
        seen.add(full_name)
        parts.append(_format_context_token(short_name, contexts[full_name], color=color))
    for name, result in contexts.items():
        if name not in seen:
            parts.append(_format_context_token(name, result, color=color))
    text = " ".join(parts) if parts else "(no contexts)"
    if req.get("last_error"):
        text += " " + _paint("[err]", _ANSI_YELLOW, color=color)
    return text


def _format_context_token(label: str, result: dict, *, color: bool) -> str:
    state = result.get("state")
    attributed = bool(result.get("attributed"))
    short = _short_state(state, attributed)
    token = f"{label}:{short}"
    if not color:
        return token
    if state == "SUCCESS" and attributed:
        return _paint(token, _ANSI_GREEN, color=True)
    if state == "FAILURE":
        return _paint(token, _ANSI_RED, color=True)
    if state in ("PENDING", None) or not attributed:
        return _paint(token, _ANSI_YELLOW, color=True)
    return _paint(token, _ANSI_RED, color=True)


def _format_state_cell(req: dict) -> str:
    if req["phase"] == "active":
        return "active"
    return req.get("outcome") or "closed"


def _format_run_cell(req: dict) -> str:
    if req.get("run_id") is None:
        return "unbound"
    if req.get("attempt") is not None:
        return f"{req['run_id']}#{req['attempt']}"
    return str(req["run_id"])


def render_status_human(
    rows: list[dict],
    counts: dict,
    poller: dict,
    *,
    verbose: bool = False,
    showing_all: bool = False,
    filtered: bool = False,
    color: bool = False,
    ref_now: float | None = None,
) -> str:
    ref_ts = now() if ref_now is None else ref_now
    if poller["status"] == "running":
        poller_text = _paint(f"running (pid {poller['pid']})", _ANSI_GREEN, color=color)
    elif poller["status"] == "stopped":
        poller_text = _paint("stopped", _ANSI_YELLOW, color=color)
    else:
        poller_text = "unknown"

    summary = f"poller: {poller_text} | active: {counts['active']}, closed: {counts['closed']}"
    if not showing_all and not filtered and counts["closed"] > 0:
        summary += " (use --recent 10, --all, or 'show <id>')"

    lines = [summary]
    if not rows:
        if not filtered and not showing_all:
            msg = "No active requests."
            if counts["closed"] > 0:
                msg += f" ({counts['closed']} closed; run `cubrid-ci-watch status --recent 10` or `--all`.)"
            lines.append(msg)
        else:
            lines.append("No matching requests.")
        return "\n".join(lines)

    table_rows = []
    for req in rows:
        table_rows.append(
            (
                f"#{req['id']}",
                f"#{req['pr']}",
                _format_state_cell(req),
                _format_run_cell(req),
                compact_checks(req, color=color),
                _deadline_label(req, ref_ts),
            )
        )

    headers = ("ID", "PR", "STATE", "RUN", "CHECKS", "TIME")
    plain_rows = [
        (
            f"#{req['id']}",
            f"#{req['pr']}",
            _format_state_cell(req),
            _format_run_cell(req),
            compact_checks(req, color=False),
            _deadline_label(req, ref_ts),
        )
        for req in rows
    ]
    widths = [
        max(len(headers[i]), *(len(r[i]) for r in plain_rows)) for i in range(6)
    ]

    lines.append("")
    lines.append(
        _paint(
            f"{headers[0]:<{widths[0]}}  {headers[1]:<{widths[1]}}  {headers[2]:<{widths[2]}}  {headers[3]:<{widths[3]}}  {headers[4]:<{widths[4]}}  {headers[5]}",
            _ANSI_BOLD,
            color=color,
        )
    )

    for req, display, plain in zip(rows, table_rows, plain_rows):
        check_pad = " " * max(0, widths[4] - len(plain[4]))
        lines.append(
            f"{display[0]:<{widths[0]}}  {display[1]:<{widths[1]}}  {display[2]:<{widths[2]}}  {display[3]:<{widths[3]}}  {display[4]}{check_pad}  {display[5]}"
        )
        if verbose:
            lines.append(
                f"  head={req['head'][:12]} kind={req['kind']} suites={','.join(req['suites'])} triggered={req['triggered_at']} deadline={req['deadline_at']}"
            )
            if req.get("last_error"):
                lines.append(f"  error: {req['last_error']}")
            obs = req.get("observation")
            if obs:
                for name, result in obs.get("contexts", {}).items():
                    status = "attributed" if result["attributed"] else result["reason"]
                    if result.get("required") is False:
                        status += ", optional"
                    lines.append(
                        f"  {name}: {result['state'] or 'not observed'} ({status})"
                    )
    return "\n".join(lines)


def render_status_plain(rows: list[dict]) -> str:
    lines = []
    for req in rows:
        lines.append(
            "\t".join(
                [
                    str(req["id"]),
                    str(req["pr"]),
                    req["phase"],
                    req.get("outcome") or "-",
                    str(req["run_id"]) if req.get("run_id") is not None else "-",
                    str(req["attempt"]) if req.get("attempt") is not None else "-",
                    ",".join(req["suites"]),
                    compact_checks(req, color=False),
                    req["triggered_at"],
                    req["deadline_at"],
                ]
            )
        )
    return "\n".join(lines)


def render_show_human(
    req: dict,
    events: list[dict],
    *,
    total_events: int,
    color: bool = False,
    ref_now: float | None = None,
) -> str:
    ref_ts = now() if ref_now is None else ref_now
    state = req["phase"] if req["phase"] == "active" else f"{req['phase']} ({req['outcome']})"
    header = _paint(f"#{req['id']} PR #{req['pr']} — {state}", _ANSI_BOLD, color=color)
    run_text = _format_run_cell(req)
    triggered_rel = relative_time(timestamp(req["triggered_at"]), ref_ts)
    deadline_rel = relative_time(timestamp(req["deadline_at"]), ref_ts)

    lines = [
        header,
        f"PR URL:     https://github.com/CUBRID/cubrid/pull/{req['pr']}",
        f"Head SHA:   {req['head']}",
        f"Kind:       {req['kind']}",
        f"Suites:     {', '.join(req['suites'])}",
        f"Run:        {run_text}",
        f"Receipt:    {req['receipt']}",
        f"Triggered:  {req['triggered_at']} ({triggered_rel})",
        f"Deadline:   {req['deadline_at']} ({deadline_rel})",
        f"Next poll:  {req['next_poll']}",
    ]
    if req.get("last_error"):
        lines.append(f"Last error: {_paint(req['last_error'], _ANSI_YELLOW, color=color)}")

    lines.append("")
    lines.append(_paint("Contexts:", _ANSI_BOLD, color=color))
    obs = req.get("observation")
    if not obs:
        lines.append("  (none yet)")
    else:
        for name, result in obs.get("contexts", {}).items():
            detail = "attributed" if result["attributed"] else result["reason"]
            if result.get("required") is False:
                detail += ", optional"
            lines.append(
                f"  {name:<18} {result['state'] or 'not observed':<12} ({detail})"
            )

    lines.append("")
    if total_events > len(events):
        lines.append(
            _paint(
                f"Events (latest {len(events)} of {total_events}; pass --all-events):",
                _ANSI_BOLD,
                color=color,
            )
        )
    else:
        lines.append(_paint(f"Events ({len(events)}):", _ANSI_BOLD, color=color))
    if not events:
        lines.append("  (none)")
    else:
        for ev in events:
            lines.append(
                f"  #{ev['id']:<4} {ev['occurred_at']}  {ev['kind']:<18} {encode(ev['payload'])}"
            )
    return "\n".join(lines)


def render_prune_human(
    candidates: list[dict],
    *,
    event_count: int,
    dry_run: bool,
    older_than: str,
    keep: int,
) -> str:
    if not candidates:
        return f"No closed requests eligible for pruning (older-than={older_than}, keep={keep})."

    ids = ", ".join(f"#{row['id']}" for row in candidates[:10])
    if len(candidates) > 10:
        ids += f", ... (+{len(candidates) - 10} more)"

    if dry_run:
        return "\n".join(
            [
                f"Dry run: would delete {len(candidates)} closed request(s) and {event_count} event(s) (older-than={older_than}, keep={keep}).",
                f"Request IDs: {ids}",
                "Run again with --yes to delete them.",
            ]
        )
    return "\n".join(
        [
            f"Deleted {len(candidates)} closed request(s) and {event_count} event(s).",
            f"Request IDs: {ids}",
        ]
    )
