#!/usr/bin/env bash
# Runs on the EC2 instance over SSH. Installs PostgreSQL on port 5001
# (local connections only), checks out the requested commit into A2-PDM,
# and starts the app under PM2. Assignment 1 directories are not touched.
set -euo pipefail

REPO_URL="$1"
GIT_SHA="$2"
INSTANCE_NAME="$3"

APP_DIR=/home/ubuntu/A2-PDM
RUNTIME_DIR=/home/ubuntu/A2-runtime
PASS_FILE="$RUNTIME_DIR/db.password"

export DEBIAN_FRONTEND=noninteractive
sudo apt-get update
sudo -E apt-get install -y git openssl postgresql

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
git -C "$APP_DIR" fetch --depth 1 origin "$GIT_SHA"
git -C "$APP_DIR" checkout --force FETCH_HEAD
echo "A2-PDM is at $(git -C "$APP_DIR" rev-parse --short HEAD)"

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"
if [ ! -s "$PASS_FILE" ]; then
  (umask 077 && openssl rand -hex 16 > "$PASS_FILE")
fi
DB_PASSWORD="$(cat "$PASS_FILE")"

PG_CONF="$(echo /etc/postgresql/*/main/postgresql.conf)"
if ! sudo grep -q '^port = 5001' "$PG_CONF"; then
  echo "port = 5001" | sudo tee -a "$PG_CONF" >/dev/null
  sudo systemctl restart postgresql
fi
for try in $(seq 1 30); do
  if sudo -u postgres psql -p 5001 -tAc 'SELECT 1' >/dev/null 2>&1; then
    break
  fi
  if [ "$try" -eq 30 ]; then
    echo "PostgreSQL did not start on port 5001." >&2
    exit 1
  fi
  sleep 2
done

sudo -u postgres psql -p 5001 -v ON_ERROR_STOP=1 -q <<SQL
DO \$\$
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'pdm_user') THEN
    ALTER ROLE pdm_user LOGIN CREATEDB PASSWORD '${DB_PASSWORD}';
  ELSE
    CREATE ROLE pdm_user LOGIN CREATEDB PASSWORD '${DB_PASSWORD}';
  END IF;
END
\$\$;
SQL
echo "PostgreSQL is listening on 127.0.0.1:5001."

DATABASE_URL="postgresql://pdm_user:${DB_PASSWORD}@127.0.0.1:5001/pdm_prod" \
PDM_INSTANCE_NAME="$INSTANCE_NAME" \
  bash "$APP_DIR/.github/scripts/deploy-app.sh"
