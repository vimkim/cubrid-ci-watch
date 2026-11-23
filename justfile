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

# Verify prerequisites, CLI, snapshot provider, and systemd service unit.
doctor:
    @PYTHONPATH=src python3 -m cubrid_ci_watch.service doctor

# Configure the systemd user service unit (does not start it).
setup *args:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service setup {{args}}

# Start the background poller service.
up:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service up

# Stop the background poller service.
down:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service down

# Restart the background poller service.
restart:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service restart

# Show systemd service state, poller status, and tracked CI requests.
status *args:
    @PYTHONPATH=src python3 -m cubrid_ci_watch.service status {{args}}

# Follow the systemd user service logs.
logs:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service logs

# Run the poller in the foreground (refuses if another poller holds poll.lock).
serve *args:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service serve {{args}}

# Enable or disable boot autostart for the background poller service.
autostart mode:
    PYTHONPATH=src python3 -m cubrid_ci_watch.service autostart {{quote(mode)}}
