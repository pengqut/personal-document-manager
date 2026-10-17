#!/usr/bin/env bash
# Delete every resource this run created. Missing ids are skipped.
set -u

STATE="${GITHUB_WORKSPACE}/pdm-lab.env"
if [ -f "$STATE" ]; then
  # shellcheck disable=SC1090
  source "$STATE"
fi

retry() {
  local try
  for try in 1 2 3 4 5 6 7 8; do
    if "$@"; then
      return 0
    fi
    sleep 15
  done
  echo "Could not finish: $*" >&2
  return 0
}

if [ -n "${MODIFIED_RULE_ID:-}" ] && [ -n "${MODIFIED_SG:-}" ]; then
  if [ -n "${OLD_SOURCE_GROUP:-}" ]; then
    aws ec2 modify-security-group-rules --group-id "$MODIFIED_SG" --security-group-rules \
      "[{\"SecurityGroupRuleId\":\"${MODIFIED_RULE_ID}\",\"SecurityGroupRule\":{\"IpProtocol\":\"${OLD_PROTO:--1}\",\"ReferencedGroupId\":\"${OLD_SOURCE_GROUP}\"}}]" || true
  elif [ -n "${OLD_CIDR:-}" ]; then
    aws ec2 modify-security-group-rules --group-id "$MODIFIED_SG" --security-group-rules \
      "[{\"SecurityGroupRuleId\":\"${MODIFIED_RULE_ID}\",\"SecurityGroupRule\":{\"IpProtocol\":\"${OLD_PROTO:--1}\",\"CidrIpv4\":\"${OLD_CIDR}\"}}]" || true
  fi
  echo "Restored the original inbound rule on ${MODIFIED_SG}."
fi
if [ "${OPENED_80:-}" = "1" ] && [ -n "${ALB_SG:-}" ]; then
  aws ec2 revoke-security-group-ingress --group-id "$ALB_SG" --protocol tcp --port 80 --cidr 0.0.0.0/0 || true
fi
if [ "${OPENED_22:-}" = "1" ] && [ -n "${ALB_SG:-}" ]; then
  aws ec2 revoke-security-group-ingress --group-id "$ALB_SG" --protocol tcp --port 22 --cidr 0.0.0.0/0 || true
fi

if [ -n "${API_ID:-}" ]; then
  aws apigatewayv2 delete-api --api-id "$API_ID" || true
fi
if [ -n "${VPC_LINK_ID:-}" ]; then
  aws apigatewayv2 delete-vpc-link --vpc-link-id "$VPC_LINK_ID" || true
fi

if [ -n "${ALB_ARN:-}" ]; then
  listeners="$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" --query 'Listeners[].ListenerArn' --output text 2>/dev/null || true)"
  for listener in $listeners; do
    aws elbv2 delete-listener --listener-arn "$listener" || true
  done
  aws elbv2 delete-load-balancer --load-balancer-arn "$ALB_ARN" || true
  aws elbv2 wait load-balancers-deleted --load-balancer-arns "$ALB_ARN" || true
fi
if [ -n "${TG_ARN:-}" ]; then
  retry aws elbv2 delete-target-group --target-group-arn "$TG_ARN" || true
fi

IDS=()
[ -n "${APP1_ID:-}" ] && IDS+=("$APP1_ID")
[ -n "${APP2_ID:-}" ] && IDS+=("$APP2_ID")
[ -n "${DB_ID:-}" ] && IDS+=("$DB_ID")
if [ "${#IDS[@]}" -gt 0 ]; then
  err="$(mktemp)"
  if aws ec2 terminate-instances --instance-ids "${IDS[@]}" 2>"$err"; then
    aws ec2 wait instance-terminated --instance-ids "${IDS[@]}" || true
  elif grep -q UnauthorizedOperation "$err"; then
    echo "TerminateInstances is denied by the student role. Stopping the instances instead."
    if ! aws ec2 stop-instances --instance-ids "${IDS[@]}"; then
      echo "StopInstances was also denied. Stop these instances in the EC2 console: ${IDS[*]}"
    fi
  else
    cat "$err" >&2
  fi
  rm -f "$err"
fi

if [ -n "${KEY_NAME:-}" ]; then
  aws ec2 delete-key-pair --key-name "$KEY_NAME" || true
fi

# The default security group belongs to the lab. Leave its rules unchanged.

rm -f "${GITHUB_WORKSPACE}/pdm-lab.pem" "$STATE"
echo "Cleanup for this run finished. Instances that cannot be terminated are stopped."
