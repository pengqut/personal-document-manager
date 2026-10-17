#!/usr/bin/env bash
# The app servers' original security group (SG_ID) can be full, so the job
# uses its own group, pdm-group19, attached to both servers alongside their
# existing groups and to the load balancer. Inbound rules on it:
#   HTTP 80 from the internet, so browsers and the load balancer can connect.
#   PostgreSQL 5001 from the same security group only.
#   SSH 22 from this runner's IP, removed again at the end of the job.
# If the group cannot be created, the rules go on SG_ID instead.
set -euo pipefail

PDM_SG_NAME=pdm-group19

PDM_SG="$(aws ec2 describe-security-groups \
  --filters "Name=vpc-id,Values=${VPC_ID}" "Name=group-name,Values=${PDM_SG_NAME}" \
  --query 'SecurityGroups[0].GroupId' --output text)"
if [ -z "$PDM_SG" ] || [ "$PDM_SG" = "None" ]; then
  if PDM_SG="$(aws ec2 create-security-group --group-name "$PDM_SG_NAME" \
      --description "PDM app servers and load balancer" --vpc-id "$VPC_ID" \
      --query GroupId --output text)"; then
    echo "Created security group ${PDM_SG_NAME} (${PDM_SG})."
  else
    echo "Could not create ${PDM_SG_NAME}. Using ${SG_ID}."
    PDM_SG="$SG_ID"
  fi
else
  echo "Found security group ${PDM_SG_NAME} (${PDM_SG})."
fi
echo "PDM_SG=${PDM_SG}" >> "$GITHUB_ENV"

if [ "$PDM_SG" != "$SG_ID" ]; then
  for instance in "$APP1_ID" "$APP2_ID"; do
    groups="$(aws ec2 describe-instances --instance-ids "$instance" \
      --query 'Reservations[0].Instances[0].SecurityGroups[].GroupId' --output text)"
    if ! grep -qw "$PDM_SG" <<<"$groups"; then
      # shellcheck disable=SC2086
      aws ec2 modify-instance-attribute --instance-id "$instance" --groups $groups "$PDM_SG"
      echo "Attached ${PDM_SG_NAME} to ${instance}."
    fi
  done
fi

add_rule() {
  local label="$1" permission="$2" out
  if out="$(aws ec2 authorize-security-group-ingress --group-id "$PDM_SG" \
      --ip-permissions "$permission" 2>&1)"; then
    echo "Added ${label}."
    return 0
  fi
  if grep -q InvalidPermission.Duplicate <<<"$out"; then
    echo "Already allowed: ${label}."
    return 1
  fi
  echo "Could not add ${label}: ${out}"
  echo "Add it to ${PDM_SG} in the EC2 console if a later step cannot connect."
  return 1
}

add_rule "HTTP 80 from 0.0.0.0/0" \
  "IpProtocol=tcp,FromPort=80,ToPort=80,IpRanges=[{CidrIp=0.0.0.0/0,Description=pdm-http}]" || true
add_rule "PostgreSQL 5001 from ${PDM_SG}" \
  "IpProtocol=tcp,FromPort=5001,ToPort=5001,UserIdGroupPairs=[{GroupId=${PDM_SG},Description=pdm-postgres}]" || true

runner_ip="$(curl -fsS https://checkip.amazonaws.com | tr -d '[:space:]')"
echo "RUNNER_CIDR=${runner_ip}/32" >> "$GITHUB_ENV"
if add_rule "SSH 22 from runner ${runner_ip}" \
    "IpProtocol=tcp,FromPort=22,ToPort=22,IpRanges=[{CidrIp=${runner_ip}/32,Description=pdm-deploy-runner}]"; then
  echo "RUNNER_SSH_RULE=1" >> "$GITHUB_ENV"
fi
