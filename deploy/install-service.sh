#!/usr/bin/env bash
# Install a user-level systemd service. No root privileges or account changes.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
python_bin="$(command -v python3)"
# The dashboard uses its own saved choices, not the CLI route configuration.
# Check its imports without reading .env or rejecting unrelated CLI budgets.
"$python_bin" - "$project_dir" <<'PY'
import sys
if sys.version_info < (3, 11):
    raise SystemExit("网页服务需要 Python 3.11 或更新版本")
sys.path.insert(0, sys.argv[1])
from flightwatch.webapp import Dashboard
PY
command -v systemctl >/dev/null
systemctl --user show-environment >/dev/null
unit_dir="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
mkdir -p -- "$unit_dir"
# systemd expands % specifiers even inside quoted paths; escape them.
safe_project="${project_dir//%/%%}"
safe_python="${python_bin//%/%%}"
cat > "$unit_dir/flight-price-watcher.service" <<EOF
[Unit]
Description=Local token-free flight price dashboard
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=$safe_project
ExecStart="$safe_python" "$safe_project/watch.py" --web --no-browser
Restart=always
RestartSec=5
UMask=0077
NoNewPrivileges=true

[Install]
WantedBy=default.target
EOF
systemd-analyze --user verify "$unit_dir/flight-price-watcher.service"
systemctl --user daemon-reload
systemctl --user enable --now flight-price-watcher.service
systemctl --user --no-pager status flight-price-watcher.service
