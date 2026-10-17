#!/usr/bin/env bash
# Install and restart this copy of the app on one EC2 instance.
# Assignment 1 stays in its own directory. This script only touches A2-PDM.
set -euo pipefail

APP_DIR=/home/ubuntu/A2-PDM
RUNTIME_DIR=/home/ubuntu/A2-runtime
ENV_FILE="$RUNTIME_DIR/A2-PDM.env"
ECOSYSTEM="$RUNTIME_DIR/ecosystem.config.cjs"

if [[ ! "${DATABASE_URL:-}" =~ ^postgres(ql)?:// ]]; then
  echo "DATABASE_URL must start with postgresql:// or postgres://" >&2
  exit 1
fi
if [[ -z "${PDM_INSTANCE_NAME:-}" ]]; then
  echo "PDM_INSTANCE_NAME is required." >&2
  exit 1
fi

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y python3-venv python3-pip nodejs npm
if ! command -v pm2 >/dev/null 2>&1; then
  sudo npm install -g pm2
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"

mkdir -p "$RUNTIME_DIR"
umask 077
cat > "$ENV_FILE" <<EOF
DATABASE_URL=${DATABASE_URL}
PDM_PORT=80
PDM_INSTANCE_NAME=${PDM_INSTANCE_NAME}
EOF
chmod 600 "$ENV_FILE"

PDM_INSTANCE_NAME="$PDM_INSTANCE_NAME" DATABASE_URL="$DATABASE_URL" \
  "$APP_DIR/.venv/bin/python" - "$ECOSYSTEM" "$APP_DIR" <<'PY'
import json
import os
import sys

destination, app_dir = sys.argv[1], sys.argv[2]
config = {
    "apps": [{
        "name": "pdm",
        "cwd": app_dir,
        "script": app_dir + "/.venv/bin/python",
        "args": "app.py",
        "interpreter": "none",
        "env": {
            "DATABASE_URL": os.environ["DATABASE_URL"],
            "PDM_PORT": "80",
            "PDM_INSTANCE_NAME": os.environ["PDM_INSTANCE_NAME"],
        },
    }]
}
with open(destination, "w", encoding="utf-8") as handle:
    handle.write("module.exports = ")
    json.dump(config, handle)
    handle.write(";\n")
PY
chmod 600 "$ECOSYSTEM"

if sudo pm2 describe pdm >/dev/null 2>&1; then
  sudo pm2 reload "$ECOSYSTEM" --update-env
else
  sudo pm2 start "$ECOSYSTEM"
fi
sudo pm2 save
sudo pm2 status

"$APP_DIR/.venv/bin/python" - <<'PY'
import json
import time
import urllib.request

for attempt in range(20):
    try:
        with urllib.request.urlopen(
            "http://127.0.0.1/api/admin/setup-status", timeout=4
        ) as response:
            body = json.loads(response.read())
            assert response.status == 200
            assert isinstance(body.get("initialized"), bool)
        print("Local health check passed.")
        break
    except Exception:
        if attempt == 19:
            raise
        time.sleep(2)
PY
