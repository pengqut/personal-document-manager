#!/usr/bin/env bash
# Finds the first app server (INSTANCE_ID) and finds or launches the second
# app server in APP2_SUBNET (a subnet Name tag or ID; picked automatically if
# empty). The subnet must be public and in another Availability Zone. The
# second server copies the first one's image, size, key pair, and security
# group. Writes the details to GITHUB_ENV for later steps.
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

aws ec2 describe-subnets --filters "Name=vpc-id,Values=${VPC_ID}" --output json > "$WORK/subnets.json"
aws ec2 describe-route-tables --filters "Name=vpc-id,Values=${VPC_ID}" --output json > "$WORK/routes.json"
CHOSEN="$(python3 - "$WORK/subnets.json" "$WORK/routes.json" "$SUBNET1" "${APP2_SUBNET:-}" <<'PY'
import json, sys
subnets = json.load(open(sys.argv[1]))["Subnets"]
tables = json.load(open(sys.argv[2]))["RouteTables"]
first, wanted = sys.argv[3], sys.argv[4].strip()

def name(subnet):
    return next((t["Value"] for t in subnet.get("Tags", []) if t["Key"] == "Name"), "")

main = next((t for t in tables if any(a.get("Main") for a in t["Associations"])), None)
explicit = {a["SubnetId"]: t for t in tables for a in t["Associations"] if a.get("SubnetId")}

def public(subnet):
    table = explicit.get(subnet["SubnetId"]) or main
    return table is not None and any(
        str(r.get("GatewayId", "")).startswith("igw-") for r in table["Routes"])

first_subnet = next(s for s in subnets if s["SubnetId"] == first)
first_az = first_subnet["AvailabilityZone"]

def fail(message):
    print(message + " Subnets in this VPC:", file=sys.stderr)
    for s in sorted(subnets, key=name):
        print(f"  {name(s) or '-':20} {s['SubnetId']}  {s['AvailabilityZone']}"
              f"  {'public' if public(s) else 'private'}", file=sys.stderr)
    sys.exit(1)

if wanted:
    matches = [s for s in subnets
               if s["SubnetId"] == wanted or name(s).lower() == wanted.lower()]
    if len(matches) != 1:
        fail(f"Subnet {wanted} was not found, or more than one subnet has that name.")
    chosen = matches[0]
    if chosen["AvailabilityZone"] == first_az:
        fail(f"Subnet {wanted} is in {first_az}, the same zone as App-1. "
             "The load balancer needs two zones.")
    if not public(chosen):
        fail(f"Subnet {wanted} has no route to an internet gateway.")
else:
    prefix = name(first_subnet).rstrip("0123456789")
    candidates = [s for s in subnets if s["AvailabilityZone"] != first_az and public(s)]
    candidates.sort(key=lambda s: (
        not (prefix and name(s).startswith(prefix)),
        not s.get("MapPublicIpOnLaunch"),
        -s["AvailableIpAddressCount"],
    ))
    if not candidates:
        fail("No public subnet in another Availability Zone.")
    chosen = candidates[0]
print(chosen["SubnetId"], chosen["AvailabilityZone"], name(chosen) or "-")
PY
)"
read -r SUBNET2 SUBNET2_AZ SUBNET2_NAME <<<"$CHOSEN"
echo "App-2 subnet ${SUBNET2_NAME} (${SUBNET2}) in ${SUBNET2_AZ}"

APP2_ID="$(aws ec2 describe-instances \
  --filters "Name=tag:Name,Values=${APP2_NAME}" "Name=vpc-id,Values=${VPC_ID}" \
            "Name=instance-state-name,Values=pending,running,stopping,stopped" \
  --query 'Reservations[].Instances[].InstanceId | [0]' --output text)"

if [ -n "$APP2_ID" ] && [ "$APP2_ID" != "None" ]; then
  current="$(aws ec2 describe-instances --instance-ids "$APP2_ID" \
    --query 'Reservations[0].Instances[0].SubnetId' --output text)"
  if [ "$current" != "$SUBNET2" ]; then
    echo "${APP2_NAME} (${APP2_ID}) is in ${current}, not ${SUBNET2}. Renaming it ${APP2_NAME}-old and stopping it."
    aws ec2 create-tags --resources "$APP2_ID" --tags "Key=Name,Value=${APP2_NAME}-old"
    aws ec2 stop-instances --instance-ids "$APP2_ID" >/dev/null \
      || echo "Could not stop ${APP2_ID}. Stop or terminate it in the EC2 console."
    APP2_ID=""
  else
    echo "Found ${APP2_NAME} (${APP2_ID})."
    start_if_stopped "$APP2_ID"
  fi
else
  APP2_ID=""
fi

if [ -z "$APP2_ID" ]; then
  echo "Launching ${APP2_NAME} in ${SUBNET2_NAME} (${SUBNET2})."
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
APP2_IP="$(aws ec2 describe-instances --instance-ids "$APP2_ID" \
  --query 'Reservations[0].Instances[0].PublicIpAddress' --output text)"
note APP1_IP "$APP1_IP"
note APP1_PRIVATE_IP "$APP1_PRIVATE_IP"
note APP2_ID "$APP2_ID"
note APP2_NAME "$APP2_NAME"
note APP2_IP "$APP2_IP"
note SUBNET2 "$SUBNET2"
echo "App-1 ${APP1_NAME} public ${APP1_IP}, private ${APP1_PRIVATE_IP}"
echo "App-2 ${APP2_NAME} public ${APP2_IP} in ${SUBNET2_NAME} (${SUBNET2})"
