#!/usr/bin/env bash
# Create the lab: VPC, two public subnets, database instance, two app instances.
# Resource ids are appended to pdm-lab.env as soon as they exist so a later
# failure can still delete them.
set -euo pipefail

STATE="${GITHUB_WORKSPACE}/pdm-lab.env"
RUN="${GITHUB_RUN_ID}"
INSTANCE_TYPE="${INSTANCE_TYPE:-t3.medium}"
: > "$STATE"

note() {
  printf '%s=%s\n' "$1" "$2" >> "$STATE"
}

note RUN_ID "$RUN"
note INSTANCE_TYPE "$INSTANCE_TYPE"

DB_PASSWORD="$(openssl rand -hex 16)"
note DB_PASSWORD "$DB_PASSWORD"
echo "::add-mask::${DB_PASSWORD}"

AMI="$(aws ssm get-parameter \
  --name /aws/service/canonical/ubuntu/server/24.04/stable/current/amd64/hvm/ebs-gp3/ami-id \
  --query Parameter.Value --output text)"
note AMI "$AMI"

read -r AZ1 AZ2 _ <<<"$(aws ec2 describe-availability-zones \
  --filters Name=state,Values=available \
  --query 'AvailabilityZones[].ZoneName' --output text)"
if [ -z "${AZ1:-}" ] || [ -z "${AZ2:-}" ]; then
  echo "This region does not have two availability zones." >&2
  exit 1
fi
note AZ1 "$AZ1"
note AZ2 "$AZ2"

VPC_ID="$(aws ec2 create-vpc --cidr-block 10.20.0.0/16 \
  --tag-specifications "ResourceType=vpc,Tags=[{Key=Name,Value=pdm-${RUN}},{Key=pdm-run,Value=${RUN}}]" \
  --query Vpc.VpcId --output text)"
note VPC_ID "$VPC_ID"
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-support '{"Value":true}'
aws ec2 modify-vpc-attribute --vpc-id "$VPC_ID" --enable-dns-hostnames '{"Value":true}'

IGW_ID="$(aws ec2 create-internet-gateway \
  --tag-specifications "ResourceType=internet-gateway,Tags=[{Key=Name,Value=pdm-igw-${RUN}},{Key=pdm-run,Value=${RUN}}]" \
  --query InternetGateway.InternetGatewayId --output text)"
note IGW_ID "$IGW_ID"
aws ec2 attach-internet-gateway --internet-gateway-id "$IGW_ID" --vpc-id "$VPC_ID"

SUBNET1="$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.20.1.0/24 \
  --availability-zone "$AZ1" \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=pdm-public-1-${RUN}},{Key=pdm-run,Value=${RUN}}]" \
  --query Subnet.SubnetId --output text)"
note SUBNET1 "$SUBNET1"
SUBNET2="$(aws ec2 create-subnet --vpc-id "$VPC_ID" --cidr-block 10.20.2.0/24 \
  --availability-zone "$AZ2" \
  --tag-specifications "ResourceType=subnet,Tags=[{Key=Name,Value=pdm-public-2-${RUN}},{Key=pdm-run,Value=${RUN}}]" \
  --query Subnet.SubnetId --output text)"
note SUBNET2 "$SUBNET2"
aws ec2 modify-subnet-attribute --subnet-id "$SUBNET1" --map-public-ip-on-launch
aws ec2 modify-subnet-attribute --subnet-id "$SUBNET2" --map-public-ip-on-launch

RT_ID="$(aws ec2 create-route-table --vpc-id "$VPC_ID" \
  --tag-specifications "ResourceType=route-table,Tags=[{Key=Name,Value=pdm-public-${RUN}},{Key=pdm-run,Value=${RUN}}]" \
  --query RouteTable.RouteTableId --output text)"
note RT_ID "$RT_ID"
aws ec2 create-route --route-table-id "$RT_ID" --destination-cidr-block 0.0.0.0/0 --gateway-id "$IGW_ID" >/dev/null
ASSOC1="$(aws ec2 associate-route-table --route-table-id "$RT_ID" --subnet-id "$SUBNET1" --query AssociationId --output text)"
ASSOC2="$(aws ec2 associate-route-table --route-table-id "$RT_ID" --subnet-id "$SUBNET2" --query AssociationId --output text)"
note ASSOC1 "$ASSOC1"
note ASSOC2 "$ASSOC2"

ALB_SG="$(aws ec2 create-security-group --vpc-id "$VPC_ID" --group-name "pdm-alb-${RUN}" \
  --description "PDM load balancer" --query GroupId --output text)"
note ALB_SG "$ALB_SG"
APP_SG="$(aws ec2 create-security-group --vpc-id "$VPC_ID" --group-name "pdm-app-${RUN}" \
  --description "PDM application" --query GroupId --output text)"
note APP_SG "$APP_SG"
DB_SG="$(aws ec2 create-security-group --vpc-id "$VPC_ID" --group-name "pdm-db-${RUN}" \
  --description "PDM database" --query GroupId --output text)"
note DB_SG "$DB_SG"
aws ec2 authorize-security-group-ingress --group-id "$ALB_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
aws ec2 authorize-security-group-ingress --group-id "$APP_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0 >/dev/null
aws ec2 authorize-security-group-ingress --group-id "$APP_SG" --protocol tcp --port 22 --cidr 0.0.0.0/0 >/dev/null
aws ec2 authorize-security-group-ingress --group-id "$DB_SG" --protocol tcp --port 5001 --source-group "$APP_SG" >/dev/null

KEY_NAME="pdm-${RUN}"
aws ec2 create-key-pair --key-name "$KEY_NAME" --query KeyMaterial --output text > "${GITHUB_WORKSPACE}/pdm-lab.pem"
chmod 600 "${GITHUB_WORKSPACE}/pdm-lab.pem"
note KEY_NAME "$KEY_NAME"

USERDATA="$(mktemp)"
python3 - "$USERDATA" "$DB_PASSWORD" <<'PY'
import sys
path, password = sys.argv[1], sys.argv[2]
script = """#!/bin/bash
set -eu
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y postgresql
PG_DIR=$(echo /etc/postgresql/*/main)
cat >> "$PG_DIR/postgresql.conf" <<'CONF'
listen_addresses = '*'
port = 5001
CONF
echo "host all pdm_user 10.20.0.0/16 scram-sha-256" >> "$PG_DIR/pg_hba.conf"
systemctl restart postgresql
for try in $(seq 1 30); do
  if sudo -u postgres psql -c 'SELECT 1' >/dev/null; then
    break
  fi
  sleep 2
done
sudo -u postgres psql -c "CREATE ROLE pdm_user LOGIN PASSWORD 'PASSWORD_TOKEN' CREATEDB"
echo "PDM-DB-$(printf READY)"
"""
open(path, "w", encoding="utf-8").write(script.replace("PASSWORD_TOKEN", password))
PY

DB_ID="$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --network-interfaces "DeviceIndex=0,SubnetId=${SUBNET1},Groups=${DB_SG},AssociatePublicIpAddress=false" \
  --user-data "file://${USERDATA}" --monitoring Enabled=true \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=PDM-DB},{Key=pdm-run,Value=${RUN}},{Key=pdm-role,Value=db}]" \
  --query 'Instances[0].InstanceId' --output text)"
rm -f "$USERDATA"
note DB_ID "$DB_ID"

launch_app() {
  local name="$1" subnet="$2" id_key="$3"
  local instance_id
  instance_id="$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" --subnet-id "$subnet" --security-group-ids "$APP_SG" \
    --monitoring Enabled=true \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=pdm-run,Value=${RUN}},{Key=pdm-role,Value=app}]" \
    --query 'Instances[0].InstanceId' --output text)"
  note "$id_key" "$instance_id"
  echo "$instance_id"
}

APP1_ID="$(launch_app PDM-App-1 "$SUBNET1" APP1_ID)"
APP2_ID="$(launch_app PDM-App-2 "$SUBNET2" APP2_ID)"

echo "Waiting for PDM-DB, PDM-App-1 and PDM-App-2 to start."
aws ec2 wait instance-running --instance-ids "$DB_ID" "$APP1_ID" "$APP2_ID"

read_ip() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].'"$2" --output text
}
wait_public_ip() {
  local instance_id="" ip="" try
  instance_id="$1"
  for try in $(seq 1 20); do
    ip="$(read_ip "$instance_id" PublicIpAddress)"
    if [ -n "$ip" ] && [ "$ip" != "None" ]; then
      printf '%s\n' "$ip"
      return 0
    fi
    sleep 5
  done
  echo "No public IP for ${instance_id}." >&2
  return 1
}
note DB_PRIVATE_IP "$(read_ip "$DB_ID" PrivateIpAddress)"
note APP1_IP "$(wait_public_ip "$APP1_ID")"
note APP2_IP "$(wait_public_ip "$APP2_ID")"

echo "Waiting for PostgreSQL on port 5001."
ready=0
for try in $(seq 1 40); do
  output="$(aws ec2 get-console-output --instance-id "$DB_ID" --latest --query Output --output text 2>/dev/null || true)"
  if printf '%s' "$output" | grep -q 'PDM-DB-READY'; then
    ready=1
    break
  fi
  sleep 15
done
if [ "$ready" -ne 1 ]; then
  echo "PostgreSQL did not finish starting." >&2
  exit 1
fi

# shellcheck disable=SC1090
source "$STATE"
ssh_base=(-i "${GITHUB_WORKSPACE}/pdm-lab.pem" -o StrictHostKeyChecking=accept-new -o UserKnownHostsFile="${RUNNER_TEMP}/pdm-known-hosts" -o ConnectTimeout=8)
for ip in "$APP1_IP" "$APP2_IP"; do
  echo "Waiting for SSH on ${ip}."
  up=0
  for try in $(seq 1 30); do
    if ssh "${ssh_base[@]}" "ubuntu@${ip}" true; then
      up=1
      break
    fi
    sleep 10
  done
  if [ "$up" -ne 1 ]; then
    echo "SSH to ${ip} did not open." >&2
    exit 1
  fi
done

echo "Lab instances are up."
echo "PDM-App-1 ${APP1_IP}"
echo "PDM-App-2 ${APP2_IP}"
echo "PDM-DB ${DB_PRIVATE_IP} port 5001"
