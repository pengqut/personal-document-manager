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

if [ -n "${API_ID:-}" ]; then
  aws apigatewayv2 delete-api --api-id "$API_ID" || true
fi
if [ -n "${VPC_LINK_ID:-}" ]; then
  aws apigatewayv2 delete-vpc-link --vpc-link-id "$VPC_LINK_ID" || true
fi

if [ -n "${ALB_ARN:-}" ]; then
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
  aws ec2 terminate-instances --instance-ids "${IDS[@]}" || true
  aws ec2 wait instance-terminated --instance-ids "${IDS[@]}" || true
fi

if [ -n "${KEY_NAME:-}" ]; then
  aws ec2 delete-key-pair --key-name "$KEY_NAME" || true
fi

# The default security group belongs to the lab. Leave its rules unchanged.

rm -f "${GITHUB_WORKSPACE}/pdm-lab.pem" "$STATE"
echo "Lab resources for this run have been deleted."
