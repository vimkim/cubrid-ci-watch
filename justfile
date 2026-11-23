# Show available commands.
default:
    @just --list

alias install := sync

# Install or refresh cubrid-ci-watch via uv for local use and daily-update-cubrid.
sync:
    #!/usr/bin/env bash
    set -euo pipefail
    export UV_TOOL_BIN_DIR="${CUBRID_CI_WATCH_BIN_DIR:-$(uv tool dir --bin)}"
    destination="$UV_TOOL_BIN_DIR/cubrid-ci-watch"
    uv tool install --reinstall .
    "$destination" --help >/dev/null
    "$destination" --version >/dev/null
    printf 'Installed %s\n' "$destination"

# Install an editable global command that follows edits in this checkout.
install-editable:
    uv tool install --force --editable "$PWD"

# Run the unit and compatibility test suite.
test *args:
    PYTHONPATH=src python3 -m unittest discover -s tests -v {{args}}

# Run tests and smoke-check the module CLI entrypoint.
check: test
    PYTHONPATH=src python3 -m cubrid_ci_watch --help >/dev/null
    PYTHONPATH=src python3 -m cubrid_ci_watch --version >/dev/null

# Show checkout, installed command path, and snapshot provider resolution.
doctor:
    @pwd
    @command -v cubrid-ci-watch
    @cubrid-ci-watch --version
    @PYTHONPATH=src python3 -c 'from cubrid_ci_watch.collector import resolve_snapshot_executable; print("snapshot:", resolve_snapshot_executable())'
