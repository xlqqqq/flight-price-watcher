#!/usr/bin/env bash
# Private local API for an HTTPS reverse proxy. Validates config before install.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$(command -v python3)"
if [[ -x "$project_dir/.venv/bin/python" ]]; then python_bin="$project_dir/.venv/bin/python"; fi
cd "$project_dir"
"$python_bin" -m flightwatch.mini_server --check
command -v systemctl >/dev/null
systemctl --user show-environment >/dev/null
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p "$unit_dir"
safe_project="${project_dir//%/%%}"
safe_python="${python_bin//%/%%}"
node_environment=""
if node_bin="$(command -v node)"; then
    safe_node="${node_bin//%/%%}"
    node_environment="Environment=\"FLIGHTWATCH_FLYAI_NODE=$safe_node\""
fi
cat > "$unit_dir/flightwatch-mini.service" <<UNIT
[Unit]
Description=Flight watcher authenticated WeChat mini-program backend
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory="$safe_project"
ExecStart="$safe_python" -m flightwatch.mini_server
$node_environment
Restart=on-failure
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
UNIT
systemd-analyze --user verify "$unit_dir/flightwatch-mini.service"
systemctl --user daemon-reload
systemctl --user enable --now flightwatch-mini.service
systemctl --user --no-pager status flightwatch-mini.service
