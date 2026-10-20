#!/usr/bin/env bash
set -euo pipefail
APP_DIR=/home/ubuntu/PC-PDM
RUNTIME_DIR=/home/ubuntu/PC-runtime
[[ "${DEPLOY_SHA:-}" =~ ^[0-9a-f]{40}$ ]]
test "$(git rev-parse HEAD)" = "$DEPLOY_SHA"
test -f "$RUNTIME_DIR/PC-PDM.env"
exec 9>"$RUNTIME_DIR/deploy.lock"
flock -n 9
mkdir -p "$RUNTIME_DIR/backups"
BACKUP="$RUNTIME_DIR/backups/pre-report-cd-$(date -u +%Y%m%dT%H%M%SZ)-$DEPLOY_SHA.tar.gz"
tar --exclude='./.venv' --exclude='./.git' --exclude='*.db' --exclude='./uploads' -czf "$BACKUP" -C "$APP_DIR" .
rsync --archive --delete --exclude='.git/' --exclude='.github/' --exclude='.venv/' --exclude='.env*' --exclude='*.db' --exclude='uploads/' --exclude='__pycache__/' --exclude='.pytest_cache/' ./ "$APP_DIR/"
"$APP_DIR/.venv/bin/python" -m pip install -r "$APP_DIR/requirements.txt"
sudo -n systemctl restart PC-PM2.service
"$APP_DIR/.venv/bin/python" - <<'PY'
import json,time,urllib.request
for attempt in range(20):
    try:
        with urllib.request.urlopen('http://127.0.0.1/api/admin/setup-status',timeout=5) as r:
            assert r.status==200
            assert isinstance(json.load(r)['initialized'],bool)
            assert r.headers.get('X-PDM-Instance') in ('PC-App-1','PC-App-2')
        break
    except Exception:
        if attempt==19:raise
        time.sleep(2)
print('Deployed tested commit; application HTTP and instance header passed.')
PY
sudo -n systemctl is-active --quiet PC-PM2.service
pm2 status
printf '%s\n' "$DEPLOY_SHA" > "$RUNTIME_DIR/deployed-commit.txt"
printf 'Deployed commit: %s\nSource backup: %s\n' "$DEPLOY_SHA" "$BACKUP"
