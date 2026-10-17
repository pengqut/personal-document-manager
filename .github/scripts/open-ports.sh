#!/usr/bin/env bash
# Inbound rules on the app servers' security group (SG_ID):
#   HTTP 80 from the internet, so browsers and the load balancer can connect.
#   PostgreSQL 5001 from the same security group only.
#   SSH 22 from this runner's IP, removed again at the end of the job.
set -euo pipefail

add_rule() {
  local label="$1" permission="$2" out
  if out="$(aws ec2 authorize-security-group-ingress --group-id "$SG_ID" \
      --ip-permissions "$permission" 2>&1)"; then
    echo "Added ${label}."
    return 0
  fi
  if grep -q InvalidPermission.Duplicate <<<"$out"; then
    echo "Already allowed: ${label}."
    return 1
  fi
  echo "Could not add ${label}: ${out}"
  echo "Add it to ${SG_ID} in the EC2 console if a later step cannot connect."
  return 1
}

add_rule "HTTP 80 from 0.0.0.0/0" \
  "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=pdm-http}]" || true
add_rule "PostgreSQL 5001 from ${SG_ID}" \
  "IpProtocol=tcp,FromPort=5001,ToPort=5001,UserIdGroupPairs=[{GroupId=${SG_ID},Description=pdm-postgres}]" || true

runner_ip="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
echo "RUNNER_CIDR=${runner_ip}/32" >> "$GITHUB_ENV"
if add_rule "SSH 22 from runner ${runner_ip}" \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${runner_ip}/32,Description=pdm-deploy-runner}]"; then
  echo "RUNNER_SSH_RULE=1" >> "$GITHUB_ENV"
fi
