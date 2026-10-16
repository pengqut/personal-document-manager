#!/usr/bin/env bash
set -euo pipefail

APP_DIR=/home/ubuntu/PC-PDM
RUNTIME_DIR=/home/ubuntu/PC-runtime
PYTHON="$APP_DIR/.venv/bin/python"

[[ "${DEPLOY_SHA:-}" =~ ^[0-9a-f]{40}$ ]]
test "$(git rev-parse HEAD)" = "$DEPLOY_SHA"
test -d "$APP_DIR"
test -x "$PYTHON"
test -f "$RUNTIME_DIR/PC-PDM.env"

# Serialize deployments and back up code only; preserve runtime settings/data.
exec 9>"$RUNTIME_DIR/deploy.lock"
flock -n 9
mkdir -p "$RUNTIME_DIR/backups"
BACKUP="$RUNTIME_DIR/backups/pre-cd-$(date -u +%Y%m%dT%H%M%SZ)-$DEPLOY_SHA.tar.gz"
tar --exclude='./.venv' --exclude='./.git' --exclude='./.env*' \
  --exclude='*.db' --exclude='./uploads' \
  -czf "$BACKUP" -C "$APP_DIR" .

rsync --archive --delete \
  --exclude='.git/' --exclude='.github/' --exclude='.venv/' \
  --exclude='.env*' --exclude='*.db' --exclude='uploads/' \
  --exclude='__pycache__/' --exclude='.pytest_cache/' \
  ./ "$APP_DIR/"
"$PYTHON" -m pip install -r "$APP_DIR/requirements.txt"
sudo -n systemctl restart PC-PDM.service

"$PYTHON" - <<'PY'
import json
import time
import urllib.request

for attempt in range(15):
    try:
        with urllib.request.urlopen(
            'http://127.0.0.1/api/admin/setup-status', timeout=4
        ) as response:
            body = json.loads(response.read())
            assert response.status == 200
            assert isinstance(body.get('initialized'), bool)
        print('Deployed application HTTP check passed.')
        break
    except Exception:
        if attempt == 14:
            raise
        time.sleep(2)
PY

sudo -n systemctl is-active --quiet PC-PDM.service
printf '%s\n' "$DEPLOY_SHA" > "$RUNTIME_DIR/deployed-commit.txt"
printf 'Deployed commit: %s\nSource backup: %s\n' "$DEPLOY_SHA" "$BACKUP"
