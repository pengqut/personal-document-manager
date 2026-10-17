#!/usr/bin/env bash
# Launch the database and both app instances in the lab VPC that already
# exists. The student role cannot create a VPC. Resource ids are appended
# to pdm-lab.env as soon as they exist so a later failure can still delete
# the instances, key pair, and security groups from this run.
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

VPC_ID="$(aws ec2 describe-vpcs --filters Name=is-default,Values=true \
  --query 'Vpcs[0].VpcId' --output text)"
if [ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ]; then
  VPC_ID="$(aws ec2 describe-vpcs --query 'Vpcs[0].VpcId' --output text)"
fi
if [ -z "$VPC_ID" ] || [ "$VPC_ID" = "None" ]; then
  echo "No VPC exists in this region, and the student role cannot create one." >&2
  exit 1
fi
VPC_CIDR="$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --query 'Vpcs[0].CidrBlock' --output text)"
note VPC_ID "$VPC_ID"
note VPC_CIDR "$VPC_CIDR"
echo "Using existing VPC ${VPC_ID} (${VPC_CIDR})."

aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --output json > subnets.json
python3 - <<'PY'
import json
import sys
subnets = json.load(open("subnets.json"))["Subnets"]
public = [item for item in subnets if item.get("MapPublicIpOnLaunch")]
pool = public or subnets
chosen = []
seen = set()
for item in pool:
    zone = item["AvailabilityZone"]
    if zone in seen:
        continue
    seen.add(zone)
    chosen.append(item)
    if len(chosen) == 2:
        break
if len(chosen) < 2:
    sys.exit("The VPC needs two subnets in different availability zones.")
with open("chosen-subnets.txt", "w", encoding="utf-8") as handle:
    for item in chosen:
        handle.write("%s %s\n" % (item["SubnetId"], item["AvailabilityZone"]))
PY
rm -f subnets.json
read -r SUBNET1 AZ1 < chosen-subnets.txt
read -r SUBNET2 AZ2 < <(sed -n '2p' chosen-subnets.txt)
rm -f chosen-subnets.txt
note SUBNET1 "$SUBNET1"
note SUBNET2 "$SUBNET2"
note AZ1 "$AZ1"
note AZ2 "$AZ2"
echo "Subnet ${SUBNET1} in ${AZ1}"
echo "Subnet ${SUBNET2} in ${AZ2}"

# The student role cannot create a security group. Use the default group
# and add SSH and HTTP rules when they are missing.
aws ec2 describe-security-groups --filters "Name=vpc-id,Values=${VPC_ID}" --output json > security-groups.json
python3 - "$VPC_CIDR" <<'PY'
import json
import sys
cidr = sys.argv[1]
groups = json.load(open("security-groups.json"))["SecurityGroups"]

def covers(perm, port):
    proto = perm.get("IpProtocol")
    if proto == "-1":
        return True
    if proto not in ("tcp", "6"):
        return False
    start, end = perm.get("FromPort"), perm.get("ToPort")
    return start is not None and end is not None and start <= port <= end

def from_world(perm):
    return any(item.get("CidrIp") == "0.0.0.0/0" for item in perm.get("IpRanges", []))

def from_self(perm, group_id):
    return any(item.get("GroupId") == group_id for item in perm.get("UserIdGroupPairs", []))

def allows_world(sg, port):
    return any(covers(perm, port) and from_world(perm) for perm in sg.get("IpPermissions", []))

def allows_self_all(sg):
    group_id = sg["GroupId"]
    return any(perm.get("IpProtocol") == "-1" and from_self(perm, group_id) for perm in sg.get("IpPermissions", []))

print("Security groups in %s:" % groups[0]["VpcId"] if groups else "No security groups.")
for sg in groups:
    print("- %s %s" % (sg["GroupId"], sg.get("GroupName")))

public = [sg for sg in groups if allows_world(sg, 22) and allows_world(sg, 80)]
public.sort(key=lambda sg: (0 if "student" in sg.get("GroupName", "").lower() else 1, sg.get("GroupName", "")))
internal = [sg for sg in groups if allows_self_all(sg)]
internal.sort(key=lambda sg: (0 if sg.get("GroupName") == "default" else 1, sg.get("GroupName", "")))
needs_ingress = "0"
if public:
    alb = public[0]
    if internal and internal[0]["GroupId"] != alb["GroupId"]:
        db = internal[0]
        app_ids = [alb["GroupId"], db["GroupId"]]
    else:
        db = alb
        app_ids = [alb["GroupId"]]
else:
    default = [sg for sg in groups if sg.get("GroupName") == "default"]
    if not default:
        sys.exit("The VPC has no default security group, and creating one is denied.")
    alb = db = default[0]
    app_ids = [alb["GroupId"]]
    needs_ingress = "1"

with open("chosen-sgs.txt", "w", encoding="utf-8") as handle:
    handle.write("ALB_SG=%s\n" % alb["GroupId"])
    handle.write("DB_SG=%s\n" % db["GroupId"])
    handle.write("APP_SGS=%s\n" % ",".join(app_ids))
    handle.write("NEEDS_INGRESS=%s\n" % needs_ingress)
print("Load balancer group: %s %s" % (alb["GroupId"], alb.get("GroupName")))
print("Database group: %s %s" % (db["GroupId"], db.get("GroupName")))
print("App groups: %s" % ",".join(app_ids))
PY
rm -f security-groups.json
# shellcheck disable=SC1091
source chosen-sgs.txt
rm -f chosen-sgs.txt
note ALB_SG "$ALB_SG"
note DB_SG "$DB_SG"
note APP_SGS "$APP_SGS"

open_port() {
  local group_id="$1" port="$2" flag="$3" err
  err="$(mktemp)"
  if aws ec2 authorize-security-group-ingress --group-id "$group_id" \
      --protocol tcp --port "$port" --cidr 0.0.0.0/0 2>"$err"; then
    note "$flag" 1
    echo "Opened tcp ${port} from the internet on ${group_id}"
  elif grep -q 'InvalidPermission.Duplicate' "$err"; then
    echo "tcp ${port} is already allowed on ${group_id}"
  else
    cat "$err" >&2
    rm -f "$err"
    exit 1
  fi
  rm -f "$err"
}

if [ "${NEEDS_INGRESS}" = "1" ]; then
  echo "The default group does not allow SSH or HTTP from the internet. Adding those two rules."
  open_port "$ALB_SG" 22 OPENED_22
  open_port "$ALB_SG" 80 OPENED_80
fi

KEY_NAME="pdm-${RUN}"
aws ec2 create-key-pair --key-name "$KEY_NAME" --query KeyMaterial --output text > "${GITHUB_WORKSPACE}/pdm-lab.pem"
chmod 600 "${GITHUB_WORKSPACE}/pdm-lab.pem"
note KEY_NAME "$KEY_NAME"

USERDATA="$(mktemp)"
python3 - "$USERDATA" "$DB_PASSWORD" "$VPC_CIDR" <<'PY'
import sys
path, password, cidr = sys.argv[1], sys.argv[2], sys.argv[3]
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
echo "host all pdm_user CIDR_TOKEN scram-sha-256" >> "$PG_DIR/pg_hba.conf"
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
open(path, "w", encoding="utf-8").write(
    script.replace("PASSWORD_TOKEN", password).replace("CIDR_TOKEN", cidr)
)
PY

DB_ID="$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
  --subnet-id "$SUBNET1" --security-group-ids "$DB_SG" --associate-public-ip-address \
  --user-data "file://${USERDATA}" --monitoring Enabled=true \
  --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=PDM-DB},{Key=pdm-run,Value=${RUN}},{Key=pdm-role,Value=db}]" \
  --query 'Instances[0].InstanceId' --output text)"
rm -f "$USERDATA"
note DB_ID "$DB_ID"

launch_app() {
  local name="$1" subnet="$2" id_key="$3"
  local instance_id
  instance_id="$(aws ec2 run-instances --image-id "$AMI" --instance-type "$INSTANCE_TYPE" \
    --key-name "$KEY_NAME" --subnet-id "$subnet" --security-group-ids "${APP_GROUP_IDS[@]}" \
    --monitoring Enabled=true \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${name}},{Key=pdm-run,Value=${RUN}},{Key=pdm-role,Value=app}]" \
    --query 'Instances[0].InstanceId' --output text)"
  note "$id_key" "$instance_id"
  echo "$instance_id"
}

IFS=',' read -r -a APP_GROUP_IDS <<< "$APP_SGS"
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
