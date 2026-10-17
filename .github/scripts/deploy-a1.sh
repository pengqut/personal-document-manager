#!/usr/bin/env bash
# Runs on an EC2 instance over SSH. Keeps Assignment 1 (tag A1_REF, default
# v1.1) in its own directory with its own SQLite database and uploads, under
# PM2 as pdm-a1 on port A1_PORT. A1's code is not changed: it has port 80
# written into app.py, so a launcher in A1-runtime starts it on A1_PORT.
# The A2 directories are not touched.
set -euo pipefail

REPO_URL="$1"
A1_REF="${2:-v1.1}"
A1_PORT="${3:-8080}"

APP_DIR=/home/ubuntu/A1-PDM
RUNTIME_DIR=/home/ubuntu/A1-runtime
LAUNCHER="$RUNTIME_DIR/run_a1.py"
ECOSYSTEM="$RUNTIME_DIR/ecosystem.config.cjs"

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
git -C "$APP_DIR" fetch --depth 1 origin "refs/tags/${A1_REF}:refs/tags/${A1_REF}"
git -C "$APP_DIR" checkout --force "$A1_REF"
echo "A1-PDM is at ${A1_REF} ($(git -C "$APP_DIR" rev-parse --short HEAD))"

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -q -r "$APP_DIR/requirements.txt"

mkdir -p "$RUNTIME_DIR"
cat > "$LAUNCHER" <<'PY'
import os
import runpy
import sys

import bottle

app_dir, port = sys.argv[1], int(sys.argv[2])
original_run = bottle.run


def run_on_a1_port(*args, **kwargs):
    kwargs["port"] = port
    original_run(*args, **kwargs)


bottle.run = run_on_a1_port
os.chdir(app_dir)
sys.path.insert(0, app_dir)
sys.argv = ["app.py"]
runpy.run_path("app.py", run_name="__main__")
PY

cat > "$ECOSYSTEM" <<EOF
module.exports = {
  apps: [{
    name: "pdm-a1",
    cwd: "${APP_DIR}",
    script: "${APP_DIR}/.venv/bin/python",
    args: "${LAUNCHER} ${APP_DIR} ${A1_PORT}",
    interpreter: "none"
  }]
};
EOF

if sudo pm2 describe pdm-a1 >/dev/null 2>&1; then
  sudo pm2 reload "$ECOSYSTEM" --update-env
else
  sudo pm2 start "$ECOSYSTEM"
fi
sudo pm2 save

for attempt in $(seq 1 20); do
  if curl -fsS -o /dev/null "http://127.0.0.1:${A1_PORT}/login.html"; then
    echo "Assignment 1 answers on port ${A1_PORT}."
    exit 0
  fi
  sleep 2
done
echo "Assignment 1 did not answer on port ${A1_PORT}." >&2
exit 1
