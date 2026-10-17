#!/usr/bin/env bash
# Attaches SECURITY_GROUP (a group name or ID in the VPC) to both app servers
# alongside their existing groups, and uses it for the load balancer.
# Needed inbound access, added only when no existing rule already covers it:
#   HTTP 80 from the internet, so browsers and the load balancer can connect.
#   PostgreSQL 5001 between the servers (same group or the VPC range).
#   SSH 22 from this runner, removed again at the end of the job.
set -euo pipefail

WORK="$(mktemp -d)"

if [[ "$SECURITY_GROUP" == sg-* ]]; then
  filter="Name=group-id,Values=${SECURITY_GROUP}"
else
  filter="Name=group-name,Values=${SECURITY_GROUP}"
fi
PDM_SG="$(aws ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=${VPC_ID}" "$filter" \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [ -z "$PDM_SG" ] || [ "$PDM_SG" = "None" ]; then
  echo "Security group ${SECURITY_GROUP} was not found in ${VPC_ID}." >&2
  exit 1
fi
echo "Using security group ${SECURITY_GROUP} (${PDM_SG})."
echo "PDM_SG=${PDM_SG}" >> "$GITHUB_ENV"

instance_groups() {
  aws ec2 describe-instances --instance-ids "$1" \
    --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId' --output text \
    | tr '\t' '\n' | sort -u
}

# App-2 and the load balancer get the same groups as App-1, so traffic that
# reaches App-1 through the class groups also reaches App-2.
app1_groups="$( (instance_groups "$APP1_ID"; echo "$PDM_SG") | sort -u)"
if [ "$(wc -l <<<"$app1_groups")" -gt 5 ]; then
  echo "App-1 would have more than 5 security groups. Remove one from ${APP1_ID} in the console." >&2
  exit 1
fi
for instance in "$APP1_ID" "$APP2_ID"; do
  current="$(instance_groups "$instance")"
  wanted="$( (echo "$current"; echo "$app1_groups") | sort -u)"
  if [ "$current" != "$wanted" ]; then
    # shellcheck disable=SC2086
    aws ec2 modify-instance-attribute --instance-id "$instance" --groups $wanted
    echo "Security groups on ${instance}: $(echo $wanted)"
  fi
done
echo "ALB_SGS=$(echo $app1_groups)" >> "$GITHUB_ENV"

runner_ip="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
echo "RUNNER_CIDR=${runner_ip}/32" >> "$GITHUB_ENV"

aws ec2 describe-security-group-rules --filters "Name=group-id,Values=${PDM_SG}" \
  --output json > "$WORK/rules.json"

covered() {
  python3 - "$WORK/rules.json" "$PDM_SG" "$VPC_CIDR" "$runner_ip" "$1" "$2" <<'PY'
import ipaddress, json, sys
rules_file, sg, vpc_cidr, runner_ip, port, need = sys.argv[1:]
port = int(port)
rules = [r for r in json.load(open(rules_file))["SecurityGroupRules"] if not r["IsEgress"]]

def opens_port(rule):
    if rule["IpProtocol"] == "-1":
        return True
    return rule["IpProtocol"] == "tcp" and rule["FromPort"] <= port <= rule["ToPort"]

def contains(cidr, target):
    try:
        return ipaddress.ip_network(target, strict=False).subnet_of(ipaddress.ip_network(cidr))
    except (TypeError, ValueError):
        return False

for rule in filter(opens_port, rules):
    cidr = rule.get("CidrIpv4")
    group = rule.get("ReferencedGroupInfo", {}).get("GroupId")
    if need == "world" and cidr == "0.0.0.0/0":
        sys.exit(0)
    if need == "internal" and (group == sg or (cidr and contains(cidr, vpc_cidr))):
        sys.exit(0)
    if need == "runner" and cidr and contains(cidr, runner_ip + "/32"):
        sys.exit(0)
sys.exit(1)
PY
}

add_rule() {
  local label="$1" permission="$2" out
  if out="$(aws ec2 authorize-security-group-ingress --group-id "$PDM_SG" \
      --ip-permissions "$permission" 2>&1)"; then
    echo "Added ${label}."
    return 0
  fi
  echo "Could not add ${label}: ${out}"
  echo "Add it to ${PDM_SG} in the EC2 console if a later step cannot connect."
  return 1
}

if covered 22 runner; then
  echo "SSH 22 is already open to the runner."
elif add_rule "SSH 22 from runner ${runner_ip}" \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${runner_ip}/32,Description=pdm-deploy-runner}]"; then
  echo "RUNNER_SSH_RULE=1" >> "$GITHUB_ENV"
fi

if covered 80 world; then
  echo "HTTP 80 is already open to the internet."
else
  add_rule "HTTP 80 from 0.0.0.0/0" \
    "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=pdm-http}]" || true
fi

if covered 5001 internal; then
  echo "PostgreSQL 5001 is already open between the servers."
else
  add_rule "PostgreSQL 5001 from ${PDM_SG}" \
    "IpProtocol=tcp,FromPort=5001,ToPort=5001,UserIdGroupPairs=[{GroupId=${PDM_SG},Description=pdm-postgres}]" || true
fi
