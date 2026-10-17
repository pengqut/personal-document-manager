#!/usr/bin/env bash
# Finds the first app server (INSTANCE_ID) and finds or launches the second
# app server in another Availability Zone of the same VPC. The second server
# copies the first one's image, size, key pair, and security group.
# Writes the details to GITHUB_ENV for later steps.
set -euo pipefail

APP2_NAME="${APP2_NAME:-Group19-App2}"
WORK="$(mktemp -d)"

note() {
  echo "$1=$2" >> "$GITHUB_ENV"
  printf -v "$1" '%s' "$2"
}

start_if_stopped() {
  local state
  state="$(aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].State.Name' --output text)"
  if [ "$state" = "stopped" ]; then
    echo "Starting $1."
    aws ec2 start-instances --instance-ids "$1" >/dev/null
  fi
}

start_if_stopped "$INSTANCE_ID"
aws ec2 describe-instances --instance-ids "$INSTANCE_ID" --output json > "$WORK/app1.json"
read -r VPC_ID SUBNET1 SG_ID AMI KEY TYPE APP1_NAME <<<"$(python3 - "$WORK/app1.json" <<'PY'
import json, sys
i = json.load(open(sys.argv[1]))["Reservations"][0]["Instances"][0]
name = next((t["Value"] for t in i.get("Tags", []) if t["Key"] == "Name"), i["InstanceId"])
print(i["VpcId"], i["SubnetId"], i["SecurityGroups"][0]["GroupId"], i["ImageId"],
      i.get("KeyName", "-"), i["InstanceType"], name)
PY
)"
if [ "$KEY" = "-" ]; then
  echo "${INSTANCE_ID} has no key pair, so the second server cannot use the same SSH key." >&2
  exit 1
fi
VPC_CIDR="$(aws ec2 describe-vpcs --vpc-ids "$VPC_ID" --query 'Vpcs[0].CidrBlock' --output text)"
note APP1_ID "$INSTANCE_ID"
note APP1_NAME "$APP1_NAME"
note VPC_ID "$VPC_ID"
note VPC_CIDR "$VPC_CIDR"
note SG_ID "$SG_ID"
note SUBNET1 "$SUBNET1"
echo "App-1 ${APP1_NAME} (${INSTANCE_ID}) in ${SUBNET1}, VPC ${VPC_ID} ${VPC_CIDR}, security group ${SG_ID}"

APP2_ID="$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${APP2_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId | [0]' --output text)"

if [ -n "$APP2_ID" ] && [ "$APP2_ID" != "None" ]; then
  echo "Found ${APP2_NAME} (${APP2_ID})."
  start_if_stopped "$APP2_ID"
else
  aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --output json > "$WORK/subnets.json"
  aws ec2 describe-route-tables --filters "Name=vpc-id,Values=${VPC_ID}" --output json > "$WORK/routes.json"
  SUBNET2="$(python3 - "$WORK/subnets.json" "$WORK/routes.json" "$SUBNET1" <<'PY'
import json, sys
subnets = json.load(open(sys.argv[1]))["Subnets"]
tables = json.load(open(sys.argv[2]))["RouteTables"]
first = sys.argv[3]

def has_igw(table):
    return any(str(r.get("GatewayId", "")).startswith("igw-") for r in table["Routes"])

def name(subnet):
    return next((t["Value"] for t in subnet.get("Tags", []) if t["Key"] == "Name"), "")

main = next((t for t in tables if any(a.get("Main") for a in t["Associations"])), None)
explicit = {a["SubnetId"]: t for t in tables for a in t["Associations"] if a.get("SubnetId")}
first_subnet = next(s for s in subnets if s["SubnetId"] == first)
first_az = first_subnet["AvailabilityZone"]
prefix = name(first_subnet).rstrip("0123456789")
candidates = [
    s for s in subnets
    if s["AvailabilityZone"] != first_az
    and (explicit.get(s["SubnetId"]) or main) is not None
    and has_igw(explicit.get(s["SubnetId"]) or main)
]
candidates.sort(key=lambda s: (
    not (prefix and name(s).startswith(prefix)),
    not s.get("MapPublicIpOnLaunch"),
    -s["AvailableIpAddressCount"],
))
print(candidates[0]["SubnetId"] if candidates else "")
PY
)"
  if [ -z "$SUBNET2" ]; then
    echo "No public subnet in another Availability Zone of ${VPC_ID}. The load balancer needs two zones." >&2
    exit 1
  fi
  echo "Launching ${APP2_NAME} in ${SUBNET2}."
  APP2_ID="$(aws ec2 run-instances \
    --image-id "$AMI" --instance-type "$TYPE" --key-name "$KEY" \
    --network-interfaces "DeviceIndex=0,SubnetId=${SUBNET2},Groups=${SG_ID},AssociatePublicIpAddress=true" \
    --tag-specifications "ResourceType=instance,Tags=[{Key=Name,Value=${APP2_NAME}}]" \
    --query 'Instances[0].InstanceId' --output text)"
  echo "Launched ${APP2_NAME} (${APP2_ID})."
fi

echo "Waiting for both servers to pass their status checks."
aws ec2 wait instance-status-ok --instance-ids "$INSTANCE_ID" "$APP2_ID"

read -r APP1_IP APP1_PRIVATE_IP <<<"$(aws ec2 describe-instances --instance-ids "$INSTANCE_ID" \
  --query 'Reservations[0].Instances[0].[PublicIpAddress,PrivateIpAddress]' --output text)"
read -r APP2_IP SUBNET2 <<<"$(aws ec2 describe-instances --instance-ids "$APP2_ID" \
  --query 'Reservations[0].Instances[0].[PublicIpAddress,SubnetId]' --output text)"
note APP1_IP "$APP1_IP"
note APP1_PRIVATE_IP "$APP1_PRIVATE_IP"
note APP2_ID "$APP2_ID"
note APP2_NAME "$APP2_NAME"
note APP2_IP "$APP2_IP"
note SUBNET2 "$SUBNET2"
echo "App-1 ${APP1_NAME} public ${APP1_IP}, private ${APP1_PRIVATE_IP}"
echo "App-2 ${APP2_NAME} public ${APP2_IP} in ${SUBNET2}"
