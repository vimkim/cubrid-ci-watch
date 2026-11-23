"""Reproducible systemd user service management for cubrid-ci-watch."""

import argparse
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from .cli import main as cli_main
from .collector import resolve_snapshot_executable
from .model import DEFAULT_WAKEUP_SECONDS
from .store import default_state_dir

OWNERSHIP_MARKER = "# managed-by: cubrid-ci-watch\n"


def service_name() -> str:
    return os.environ.get("CUBRID_CI_WATCH_SERVICE_NAME", "cubrid-ci-watch.service")


def unit_dir() -> Path:
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg) if xdg else Path.home() / ".config"
    return base / "systemd/user"


def unit_path() -> Path:
    return unit_dir() / service_name()


def escape_systemd(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def resolve_cli_executable() -> str:
    found = shutil.which("cubrid-ci-watch")
    if not found:
        raise ValueError("cubrid-ci-watch not found on PATH; run just sync")
    return found


def resolve_gh_executable() -> str:
    gh_name = os.environ.get("CUBRID_CI_WATCH_GH", "gh")
    if os.path.isabs(gh_name):
        if os.path.isfile(gh_name) and os.access(gh_name, os.X_OK):
            return gh_name
        raise ValueError(f"gh executable not found or not executable: {gh_name}")
    found = shutil.which(gh_name)
    if not found:
        raise ValueError(f"gh executable not found on PATH: {gh_name}")
    return found


def resolve_runtime() -> dict[str, str | None]:
    cli = resolve_cli_executable()
    snapshot = resolve_snapshot_executable()
    gh = resolve_gh_executable()
    gh_pr_info = shutil.which("gh-pr-info")
    dirs: list[str] = []
    for exe in (cli, snapshot, gh, gh_pr_info):
        if exe:
            parent = str(Path(exe).parent)
            if parent not in dirs:
                dirs.append(parent)
    for entry in os.defpath.split(os.pathsep):
        if entry and entry not in dirs:
            dirs.append(entry)
    return {
        "cli": cli,
        "snapshot": snapshot,
        "gh": gh,
        "gh_pr_info": gh_pr_info,
        "path": os.pathsep.join(dirs),
    }


def render_unit(
    *,
    cli: str,
    state_dir: Path,
    wakeup: int,
    path_env: str,
    snapshot: str,
    gh: str,
    interval: int | None = None,
) -> str:
    exec_parts = [cli, "--state-dir", str(state_dir), "poll", "--wakeup", str(wakeup)]
    if interval is not None:
        exec_parts += ["--interval", str(interval)]
    exec_start = " ".join(escape_systemd(part) for part in exec_parts)
    return (
        f"{OWNERSHIP_MARKER}"
        "[Unit]\n"
        "Description=CUBRID CI Watch Poller\n"
        "After=network-online.target\n"
        "Wants=network-online.target\n\n"
        "[Service]\n"
        "Type=simple\n"
        f"ExecStart={exec_start}\n"
        f"Environment={escape_systemd('PATH=' + path_env)}\n"
        f"Environment={escape_systemd('CUBRID_CI_WATCH_SNAPSHOT=' + snapshot)}\n"
        f"Environment={escape_systemd('CUBRID_CI_WATCH_GH=' + gh)}\n"
        "Restart=on-failure\n"
        "RestartSec=5\n"
        "KillSignal=SIGTERM\n"
        "TimeoutStopSec=15\n"
        "StandardOutput=journal\n"
        "StandardError=journal\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )


def ensure_owned(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"refusing symlink service unit: {path}")
    if path.exists() and not path.read_text().startswith(OWNERSHIP_MARKER):
        raise ValueError(f"foreign service unit at {path}; inspect before replacing")


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=".unit-")
    try:
        with os.fdopen(fd, "w") as out:
            out.write(text)
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def run_cmd(argv: list[str], *, check: bool = True) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{Path(argv[0]).name} unavailable or timed out") from exc
    if check and proc.returncode:
        detail = proc.stderr.strip() or proc.stdout.strip()
        suffix = f": {detail}" if detail else ""
        raise ValueError(f"{' '.join(argv)} failed ({proc.returncode}){suffix}")
    return proc


def _require_linux_systemd() -> None:
    if sys.platform != "linux":
        raise ValueError("systemd user service requires Linux")
    for tool in ("systemctl", "systemd-analyze"):
        if not shutil.which(tool):
            raise ValueError(f"missing prerequisite: {tool}")


def setup_service(
    *,
    state_dir: Path | None = None,
    wakeup: int = DEFAULT_WAKEUP_SECONDS,
    interval: int | None = None,
) -> Path:
    _require_linux_systemd()
    if wakeup <= 0 or (interval is not None and interval <= 0):
        raise ValueError("--wakeup and --interval must be positive")
    runtime = resolve_runtime()
    resolved_state = (state_dir or default_state_dir()).expanduser().resolve()
    target = unit_path()
    ensure_owned(target)
    body = render_unit(
        cli=runtime["cli"],
        state_dir=resolved_state,
        wakeup=wakeup,
        path_env=runtime["path"],
        snapshot=runtime["snapshot"],
        gh=runtime["gh"],
        interval=interval,
    )
    with tempfile.TemporaryDirectory(prefix="cubrid-ci-watch-unit-") as tmpdir:
        staged = Path(tmpdir) / service_name()
        staged.write_text(body)
        run_cmd(["systemd-analyze", "--user", "verify", str(staged)])
    if not target.exists() or target.read_text() != body:
        atomic_write(target, body)
    run_cmd(["systemctl", "--user", "daemon-reload"])
    print(f"Configured {target}. Start with: just up")
    return target


def lifecycle(action: str) -> None:
    _require_linux_systemd()
    target = unit_path()
    if not target.exists():
        raise ValueError(f"service unit missing ({target}); run just setup")
    ensure_owned(target)
    name = service_name()
    verb = {"up": "start", "down": "stop", "restart": "restart"}[action]
    run_cmd(["systemctl", "--user", verb, name])
    if action in ("up", "restart"):
        state = run_cmd(
            ["systemctl", "--user", "show", name, "--property=ActiveState", "--value"]
        ).stdout.strip()
        if state != "active":
            raise ValueError(f"{name} did not become active ({state}); inspect with just logs")
    print(f"{action} completed ({name})")


def show_status(*, state_dir: Path | None = None) -> int:
    name = service_name()
    target = unit_path()
    if sys.platform == "linux" and shutil.which("systemctl"):
        if not target.exists():
            print(f"service: not configured ({target})")
        else:
            proc = run_cmd(
                [
                    "systemctl",
                    "--user",
                    "show",
                    name,
                    "--property=LoadState,ActiveState,SubState,UnitFileState,MainPID",
                ],
                check=False,
            )
            if proc.returncode:
                print(f"service: unavailable ({name})")
            else:
                props = dict(
                    line.split("=", 1)
                    for line in proc.stdout.splitlines()
                    if "=" in line
                )
                print(
                    f"service: {name} active={props.get('ActiveState', 'unknown')}/"
                    f"{props.get('SubState', 'unknown')} "
                    f"enabled={props.get('UnitFileState', 'unknown')} "
                    f"pid={props.get('MainPID', '0')}"
                )
    cli_args = ["--state-dir", str(state_dir)] if state_dir else []
    return cli_main([*cli_args, "status"])


def run_doctor(*, state_dir: Path | None = None) -> int:
    ok = True

    def check(label: str, fn, remedy: str) -> str | None:
        nonlocal ok
        try:
            detail = fn()
            suffix = f" ({detail})" if detail else ""
            print(f"ok {label}{suffix}")
            return detail
        except (ValueError, OSError) as exc:
            ok = False
            print(f"ERROR {label}: {exc}. {remedy}")
            return None

    def check_cli():
        exe = resolve_cli_executable()
        ver = run_cmd([exe, "--version"]).stdout.strip()
        return f"{exe} [{ver}]"

    def check_gh():
        gh = resolve_gh_executable()
        run_cmd([gh, "auth", "status"])
        return gh

    def check_gh_pr_info():
        found = shutil.which("gh-pr-info")
        if not found:
            raise ValueError("gh-pr-info missing from PATH")
        return found

    check("cubrid-ci-watch CLI", check_cli, "Run just sync")
    check("cubrid-pr-status snapshot provider", resolve_snapshot_executable, "Put cubrid-pr-status on PATH or set CUBRID_CI_WATCH_SNAPSHOT")
    check("gh authentication", check_gh, "Install gh and run gh auth login")
    check("gh-pr-info", check_gh_pr_info, "Install my-git-utils and ensure gh-pr-info is on PATH")

    target = unit_path()
    name = service_name()
    if not target.exists():
        print(f"NOTE service unit not set up yet ({target}); run just setup")
    else:
        def check_unit():
            ensure_owned(target)
            props = dict(
                line.split("=", 1)
                for line in run_cmd(
                    ["systemctl", "--user", "show", name, "--property=LoadState,ActiveState,UnitFileState"]
                ).stdout.splitlines()
                if "=" in line
            )
            if props.get("LoadState") != "loaded":
                raise ValueError(f"LoadState={props.get('LoadState', 'unknown')}")
            return f"active={props.get('ActiveState')} enabled={props.get('UnitFileState')}"

        check("systemd user unit", check_unit, "Run just setup")

    resolved_state = (state_dir or default_state_dir()).expanduser().resolve()
    print(f"state_dir: {resolved_state}")
    return 0 if ok else 1


def autostart(mode: str) -> None:
    _require_linux_systemd()
    target = unit_path()
    if not target.exists():
        raise ValueError(f"service unit missing ({target}); run just setup")
    ensure_owned(target)
    name = service_name()
    if mode == "enable":
        linger = run_cmd(
            ["loginctl", "show-user", str(os.getuid()), "--property=Linger", "--value"]
        ).stdout.strip()
        if linger != "yes":
            raise ValueError(
                f"boot autostart requires user linger; run loginctl enable-linger {getpass.getuser()}"
            )
    run_cmd(["systemctl", "--user", mode, name])
    print(f"autostart {mode} completed ({name})")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage cubrid-ci-watch systemd user service.")
    sub = parser.add_subparsers(dest="action", required=True)

    setup_p = sub.add_parser("setup")
    setup_p.add_argument("--state-dir", type=Path, default=None)
    setup_p.add_argument("--wakeup", type=int, default=DEFAULT_WAKEUP_SECONDS)
    setup_p.add_argument("--interval", type=int, default=None)

    for act in ("up", "down", "restart", "logs"):
        sub.add_parser(act)

    status_p = sub.add_parser("status")
    status_p.add_argument("--state-dir", type=Path, default=None)

    doctor_p = sub.add_parser("doctor")
    doctor_p.add_argument("--state-dir", type=Path, default=None)

    serve_p = sub.add_parser("serve")
    serve_p.add_argument("--state-dir", type=Path, default=None)
    serve_p.add_argument("--wakeup", type=int, default=DEFAULT_WAKEUP_SECONDS)
    serve_p.add_argument("--interval", type=int, default=None)

    auto_p = sub.add_parser("autostart")
    auto_p.add_argument("mode", choices=("enable", "disable"))

    args = parser.parse_args(argv)
    try:
        if args.action == "setup":
            setup_service(state_dir=args.state_dir, wakeup=args.wakeup, interval=args.interval)
            return 0
        if args.action in ("up", "down", "restart"):
            lifecycle(args.action)
            return 0
        if args.action == "status":
            return show_status(state_dir=args.state_dir)
        if args.action == "doctor":
            return run_doctor(state_dir=args.state_dir)
        if args.action == "logs":
            _require_linux_systemd()
            os.execvp(
                "journalctl",
                ["journalctl", "--user", "-u", service_name(), "-n", "100", "-f"],
            )
        if args.action == "serve":
            cli_args = []
            if args.state_dir:
                cli_args += ["--state-dir", str(args.state_dir)]
            cli_args += ["poll", "--wakeup", str(args.wakeup)]
            if args.interval is not None:
                cli_args += ["--interval", str(args.interval)]
            return cli_main(cli_args)
        if args.action == "autostart":
            autostart(args.mode)
            return 0
        return 2
    except ValueError as exc:
        print(f"cubrid-ci-watch service: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
