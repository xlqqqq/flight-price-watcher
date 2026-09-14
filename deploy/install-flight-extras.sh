#!/usr/bin/env bash
# Optional official FlyAI client and anonymous browser; no account enrollment.
set -euo pipefail
project_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
command -v node >/dev/null
command -v npm >/dev/null
node -e 'if (Number(process.versions.node.split(".")[0]) < 18) process.exit(1)'
python3 -m venv --system-site-packages "$project_dir/.venv"
"$project_dir/.venv/bin/python" -m pip install -r "$project_dir/requirements-browser.txt"
"$project_dir/.venv/bin/python" -m playwright install chromium
npm install --prefix "$project_dir/.runtime/flyai" --ignore-scripts --no-audit --no-fund --save-exact @fly-ai/flyai-cli@1.0.16
printf '%s\n' '安装完成。FlyAI 查询备用入口已可用；普通浏览器可设 FLIGHTWATCH_FLIGGY_BROWSER=1 启用。'
