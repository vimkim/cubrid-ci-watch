"""Command-line interface entry point for cubrid-ci-watch."""

import argparse
import os
from pathlib import Path
import sqlite3
import sys

from . import __version__
from .collector import poll_loop, wait_for_events
from .format import (
    render_prune_human,
    render_show_human,
    render_status_human,
    render_status_plain,
    resolve_color,
)
from .model import DEFAULT_WAKEUP_SECONDS, encode, parse_duration, parse_pr_number
from .store import (
    connect,
    count_events_for_request,
    default_state_dir,
    get_request,
    inspect_poller,
    prune_requests,
    query_events,
    query_requests,
    register_request,
    request_counts,
    select_prunable_requests,
)


def _pr_arg(value: str) -> int:
    try:
        return parse_pr_number(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def _positive_int(value: str) -> int:
    try:
        num = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if num <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return num


def _nonnegative_int(value: str) -> int:
    try:
        num = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if num < 0:
        raise argparse.ArgumentTypeError("must be nonnegative")
    return num


def _build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--state-dir",
        type=Path,
        default=argparse.SUPPRESS,
        help="ledger directory (default: $XDG_STATE_HOME/cubrid-ci-watch)",
    )
    common.add_argument(
        "--no-color",
        action="store_true",
        default=argparse.SUPPRESS,
        help="disable ANSI colors in human output",
    )

    epilog = """examples:
  cubrid-ci-watch status                    # active requests + poller status
  cubrid-ci-watch status --recent 10        # 10 most recent requests
  cubrid-ci-watch show 87                   # inspect one request and its events
  cubrid-ci-watch poll                      # run shared background poller
  cubrid-ci-watch wait 87 --after 300       # block until next meaningful event
  cubrid-ci-watch prune --older-than 30d    # preview closed requests to prune

exit codes:
  0  command succeeded (or wait observed a matching event / closed request)
  2  invalid usage, conflicting registration, competing poller, or poll --once error
  3  wait --timeout elapsed before a matching event or closure"""

    parser = argparse.ArgumentParser(
        prog="cubrid-ci-watch",
        description="Track caller-identified CUBRID gha-ci requests in a local SQLite ledger.",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
        parents=[common],
    )
    parser.add_argument(
        "-V", "--version", action="version", version=f"%(prog)s {__version__}"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    reg = sub.add_parser(
        "register",
        parents=[common],
        help="register or bind a triggered CI request",
        description="Register a triggered CI request idempotently, or bind run_id/attempt once.",
    )
    reg.add_argument("pr", type=_pr_arg, help="PR number or canonical CUBRID PR URL")
    reg.add_argument("--head", required=True, help="40-character PR head commit SHA")
    reg.add_argument(
        "--suite",
        action="append",
        required=True,
        help="suite to track (shell, sql, medium, sql_by_cci); repeatable",
    )
    reg.add_argument(
        "--triggered-at", required=True, help="ISO8601 trigger time with timezone"
    )
    reg.add_argument("--receipt", required=True, help="unique operation receipt ID")
    reg.add_argument(
        "--kind",
        choices=["run", "rerun-failed"],
        default="run",
        help="trigger mechanism (default: run)",
    )
    reg.add_argument("--run-id", type=int, help="GitHub Actions run ID")
    reg.add_argument("--attempt", type=int, help="GitHub Actions run attempt")

    poll_p = sub.add_parser(
        "poll",
        parents=[common],
        help="poll active requests and stream new JSONL events",
        description="Poll due active requests while holding poll.lock and emit new events as JSONL.",
    )
    poll_p.add_argument(
        "--once", action="store_true", help="run a single poll pass and exit"
    )
    poll_p.add_argument(
        "--force",
        action="store_true",
        help="refresh all active requests regardless of next_poll (requires --once)",
    )
    poll_p.add_argument(
        "--interval",
        type=int,
        default=None,
        help="fixed poll interval in seconds (default: per-PR 1/2/4/8/16m backoff)",
    )
    poll_p.add_argument(
        "--wakeup",
        type=int,
        default=DEFAULT_WAKEUP_SECONDS,
        help="poller wakeup check interval in seconds (default: 10)",
    )

    status_p = sub.add_parser(
        "status",
        aliases=["ls"],
        parents=[common],
        help="show tracked requests (active by default in human view)",
        description="Show active requests in human view by default; --json without filters returns all requests for compatibility.",
    )
    scope = status_p.add_mutually_exclusive_group()
    scope.add_argument(
        "--active",
        dest="scope",
        action="store_const",
        const="active",
        help="show only active requests",
    )
    scope.add_argument(
        "--closed",
        dest="scope",
        action="store_const",
        const="closed",
        help="show only closed requests",
    )
    scope.add_argument(
        "--all",
        dest="scope",
        action="store_const",
        const="all",
        help="show both active and closed requests",
    )
    status_p.add_argument("--pr", type=_pr_arg, help="filter by PR number or URL")
    status_p.add_argument(
        "--outcome",
        choices=["success", "failure", "no_verdict", "head_changed", "superseded", "deadline"],
        help="filter by closed outcome",
    )
    status_p.add_argument(
        "--recent",
        "--limit",
        dest="recent",
        type=_positive_int,
        help="show the N most recent matching requests",
    )
    status_p.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="include head SHA, timestamps, errors, and full context attribution",
    )
    fmt = status_p.add_mutually_exclusive_group()
    fmt.add_argument("--json", dest="format", action="store_const", const="json", help="emit structured JSON")
    fmt.add_argument("--plain", dest="format", action="store_const", const="plain", help="emit tab-separated flat rows")

    show_p = sub.add_parser(
        "show",
        parents=[common],
        help="inspect one request in detail",
        description="Show full metadata, context attribution, and recorded events for one request.",
    )
    show_p.add_argument("request_id", type=_positive_int, help="registered request ID")
    show_p.add_argument(
        "--all-events",
        action="store_true",
        help="show all recorded events instead of only the latest 10",
    )
    show_p.add_argument("--json", action="store_true", help="emit structured JSON")

    events_p = sub.add_parser(
        "events",
        parents=[common],
        help="replay recorded events as JSONL",
        description="Emit recorded events with ID > --after as newline-delimited JSON.",
    )
    events_p.add_argument("--after", type=int, default=0, help="event ID cursor (default: 0)")
    events_p.add_argument("--request", type=_positive_int, help="filter by request ID")
    events_p.add_argument("--kind", help="filter by event kind")
    events_p.add_argument("--limit", type=_positive_int, help="limit number of emitted events")

    wait_p = sub.add_parser(
        "wait",
        parents=[common],
        help="block until a meaningful or terminal event commits for one request",
        description="Wait for a meaningful or terminal event for one request, cooperating with poll.lock.",
    )
    wait_p.add_argument("request_id", type=int, help="registered request ID")
    wait_p.add_argument(
        "--after",
        type=int,
        default=0,
        help="only emit events with ID greater than this cursor (default: 0)",
    )
    wait_p.add_argument(
        "--until",
        choices=["meaningful", "terminal"],
        default="meaningful",
        help="event class that wakes the wait (default: meaningful)",
    )
    wait_p.add_argument(
        "--interval",
        type=int,
        default=None,
        help="fixed fallback poll interval in seconds when no poller holds poll.lock (default: per-PR 1/2/4/8/16m backoff)",
    )
    wait_p.add_argument(
        "--timeout",
        type=int,
        default=0,
        help="seconds before exiting 3; 0 waits until event or closure (default: 0)",
    )

    prune_p = sub.add_parser(
        "prune",
        parents=[common],
        help="preview or remove old closed requests and their events",
        description="Remove closed requests older than --older-than. Dry-run by default unless --yes is passed.",
    )
    prune_p.add_argument(
        "--older-than",
        default="30d",
        help="age threshold based on triggered_at, e.g. 30d, 12h, 2w (default: 30d)",
    )
    prune_p.add_argument(
        "--keep",
        type=_nonnegative_int,
        default=0,
        help="always keep the N newest closed requests (default: 0)",
    )
    prune_p.add_argument(
        "--dry-run",
        action="store_true",
        help="preview prunable rows without modifying the ledger (default without --yes)",
    )
    prune_p.add_argument(
        "-y",
        "--yes",
        action="store_true",
        help="actually delete matching closed requests and their events",
    )
    prune_p.add_argument("--json", action="store_true", help="emit structured JSON")

    return parser


def _handle_status(db: sqlite3.Connection, state_dir: Path, args, *, color: bool) -> int:
    fmt = getattr(args, "format", None) or "human"
    scope = getattr(args, "scope", None)
    explicit_filter = any(
        x is not None for x in (scope, args.pr, args.outcome, args.recent)
    )

    if fmt == "json" and not explicit_filter:
        rows = query_requests(db, order="asc")
        print(encode({"schema_version": 1, "requests": rows}))
        return 0

    if fmt == "json":
        phase = None if scope in (None, "all") else scope
        order = "desc" if args.recent is not None else "asc"
        rows = query_requests(
            db,
            phase=phase,
            pr=args.pr,
            outcome=args.outcome,
            order=order,
            limit=args.recent,
        )
        print(encode({"schema_version": 1, "requests": rows}))
        return 0

    if not explicit_filter:
        phase = "active"
        showing_all = False
    elif scope == "active":
        phase = "active"
        showing_all = False
    elif scope == "closed":
        phase = "closed"
        showing_all = True
    else:
        phase = None
        showing_all = scope == "all"

    rows = query_requests(
        db,
        phase=phase,
        pr=args.pr,
        outcome=args.outcome,
        order="desc",
        limit=args.recent,
    )
    if fmt == "plain":
        text = render_status_plain(rows)
        if text:
            print(text)
        return 0

    counts = request_counts(db)
    poller = inspect_poller(state_dir)
    print(
        render_status_human(
            rows,
            counts,
            poller,
            verbose=args.verbose,
            showing_all=showing_all,
            filtered=explicit_filter and scope is None,
            color=color,
        )
    )
    return 0


def _handle_show(db: sqlite3.Connection, args, *, color: bool) -> int:
    req = get_request(db, args.request_id)
    if req is None:
        raise ValueError(f"unknown request {args.request_id}")
    if args.json:
        events = query_events(db, request_id=args.request_id)
        print(encode({"schema_version": 1, "request": req, "events": events}))
        return 0

    total = count_events_for_request(db, args.request_id)
    if args.all_events:
        events = query_events(db, request_id=args.request_id)
    else:
        events = query_events(
            db, request_id=args.request_id, limit=10, newest_first=True
        )
    print(render_show_human(req, events, total_events=total, color=color))
    return 0


def _handle_prune(db: sqlite3.Connection, args) -> int:
    seconds = parse_duration(args.older_than)
    dry_run = args.dry_run or not args.yes
    candidates, event_count = select_prunable_requests(
        db, older_than_seconds=seconds, keep=args.keep
    )
    ids = [row["id"] for row in candidates]
    if dry_run:
        deleted_requests, deleted_events = len(ids), event_count
    else:
        deleted_requests, deleted_events = prune_requests(db, ids)

    if args.json:
        print(
            encode(
                {
                    "schema_version": 1,
                    "dry_run": dry_run,
                    "deleted_requests": deleted_requests,
                    "deleted_events": deleted_events,
                    "request_ids": ids,
                }
            )
        )
    else:
        print(
            render_prune_human(
                candidates,
                event_count=deleted_events if not dry_run else event_count,
                dry_run=dry_run,
                older_than=args.older_than,
                keep=args.keep,
            )
        )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    state_dir = getattr(args, "state_dir", None) or default_state_dir()
    no_color = bool(getattr(args, "no_color", False))
    color = resolve_color(no_color_flag=no_color, stream=sys.stdout)

    try:
        db = connect(state_dir)
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"cubrid-ci-watch: {exc}", file=sys.stderr)
        return 2

    try:
        if args.command == "register":
            request_id = register_request(
                db,
                pr=args.pr,
                head=args.head,
                suites=args.suite,
                triggered_at=args.triggered_at,
                receipt=args.receipt,
                kind=args.kind,
                run_id=args.run_id,
                attempt=args.attempt,
            )
            print(encode({"schema_version": 1, "request_id": request_id}))
            return 0
        if args.command == "poll":
            return poll_loop(
                db,
                state_dir,
                once=args.once,
                force=args.force,
                interval=args.interval,
                wakeup=args.wakeup,
                on_events=lambda evs: [print(encode(ev), flush=True) for ev in evs],
            )
        if args.command in ("status", "ls"):
            return _handle_status(db, state_dir, args, color=color)
        if args.command == "show":
            return _handle_show(db, args, color=color)
        if args.command == "events":
            for ev in query_events(
                db,
                after=args.after,
                request_id=args.request,
                kind=args.kind,
                limit=args.limit,
            ):
                print(encode(ev), flush=True)
            return 0
        if args.command == "wait":
            code, matched, notice = wait_for_events(
                db,
                state_dir,
                request_id=args.request_id,
                after=args.after,
                until=args.until,
                interval=args.interval,
                timeout=args.timeout,
            )
            for ev in matched:
                print(encode(ev), flush=True)
            if notice:
                print(notice, file=sys.stderr)
            return code
        if args.command == "prune":
            return _handle_prune(db, args)
        return 2
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(f"cubrid-ci-watch: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 0
    finally:
        db.close()
