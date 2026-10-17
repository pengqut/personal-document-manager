#!/usr/bin/env bash
# Runs on an EC2 instance over SSH. Checks out the requested commit into
# A2-PDM and starts the app under PM2. Assignment 1 directories are not touched.
#
# Role "db": also installs PostgreSQL on port 5001, accepts connections from
# VPC_CIDR, and the app uses the local database.
# Role "app": the app uses DATABASE_URL, which the caller exports first.
set -euo pipefail

REPO_URL="$1"
GIT_SHA="$2"
INSTANCE_NAME="$3"
ROLE="${4:-db}"
VPC_CIDR="${5:-}"

APP_DIR=/home/ubuntu/A2-PDM
RUNTIME_DIR=/home/ubuntu/A2-runtime
PASS_FILE="$RUNTIME_DIR/db.password"
APT=(sudo -E apt-get -o DPkg::Lock::Timeout=300)

cloud-init status --wait >/dev/null 2>&1 || true
export DEBIAN_FRONTEND=noninteractive
"${APT[@]}" update
"${APT[@]}" install -y git openssl

if [ ! -d "$APP_DIR/.git" ]; then
  git clone "$REPO_URL" "$APP_DIR"
fi
git -C "$APP_DIR" fetch --depth 1 origin "$GIT_SHA"
git -C "$APP_DIR" checkout --force FETCH_HEAD
echo "A2-PDM is at $(git -C "$APP_DIR" rev-parse --short HEAD)"

mkdir -p "$RUNTIME_DIR"
chmod 700 "$RUNTIME_DIR"

if [ "$ROLE" = "db" ]; then
  "${APT[@]}" install -y postgresql
  if [ ! -s "$PASS_FILE" ]; then
    (umask 077 && openssl rand -hex 16 > "$PASS_FILE")
  fi
  DB_PASSWORD="$(cat "$PASS_FILE")"

  PG_DIR="$(echo /etc/postgresql/*/main)"
  changed=0
  if ! sudo grep -q '^port = 5001' "$PG_DIR/postgresql.conf"; then
    echo "port = 5001" | sudo tee -a "$PG_DIR/postgresql.conf" >/dev/null
    changed=1
  fi
  if [ -n "$VPC_CIDR" ]; then
    if ! sudo grep -q "^listen_addresses = '\*'" "$PG_DIR/postgresql.conf"; then
      echo "listen_addresses = '*'" | sudo tee -a "$PG_DIR/postgresql.conf" >/dev/null
      changed=1
    fi
    rule="host all pdm_user ${VPC_CIDR} scram-sha-256"
    if ! sudo grep -qF "$rule" "$PG_DIR/pg_hba.conf"; then
      echo "$rule" | sudo tee -a "$PG_DIR/pg_hba.conf" >/dev/null
      changed=1
    fi
  fi
  if [ "$changed" -eq 1 ]; then
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
  echo "PostgreSQL is listening on port 5001${VPC_CIDR:+ for ${VPC_CIDR}}."
  DATABASE_URL="postgresql://pdm_user:${DB_PASSWORD}@127.0.0.1:5001/pdm_prod"
elif [ "$ROLE" = "app" ]; then
  if [ -z "${DATABASE_URL:-}" ]; then
    echo "DATABASE_URL is required for the app role." >&2
    exit 1
  fi
else
  echo "Unknown role ${ROLE}. Use db or app." >&2
  exit 1
fi

DATABASE_URL="$DATABASE_URL" PDM_INSTANCE_NAME="$INSTANCE_NAME" \
  bash "$APP_DIR/.github/scripts/deploy-app.sh"
