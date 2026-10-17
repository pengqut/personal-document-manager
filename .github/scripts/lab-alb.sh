#!/usr/bin/env bash
# Create the Application Load Balancer in front of the two app instances.
set -euo pipefail

STATE="${GITHUB_WORKSPACE}/pdm-lab.env"
# shellcheck disable=SC1090
source "$STATE"

note() {
  printf '%s=%s\n' "$1" "$2" >> "$STATE"
}

TG_ARN="$(aws elbv2 create-target-group --name "pdm-tg-${RUN_ID}" \
  --protocol HTTP --port 80 --vpc-id "$VPC_ID" --target-type instance \
  --health-check-protocol HTTP --health-check-path /api/admin/setup-status \
  --health-check-interval-seconds 15 --healthy-threshold-count 2 \
  --unhealthy-threshold-count 3 --matcher HttpCode=200 \
  --query 'TargetGroups[0].TargetGroupArn' --output text)"
note TG_ARN "$TG_ARN"
aws elbv2 register-targets --target-group-arn "$TG_ARN" \
  --targets "Id=${APP1_ID},Port=80" "Id=${APP2_ID},Port=80"

ALB_ARN="$(aws elbv2 create-load-balancer --name "pdm-alb-${RUN_ID}" \
  --subnets "$SUBNET1" "$SUBNET2" --security-groups "$ALB_SG" \
  --scheme internet-facing --type application \
  --tags Key=pdm-run,Value="$RUN_ID" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
note ALB_ARN "$ALB_ARN"
aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
  --protocol HTTP --port 80 \
  --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" >/dev/null

aws elbv2 wait load-balancer-available --load-balancer-arns "$ALB_ARN"
ALB_DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)"
note ALB_DNS "$ALB_DNS"
echo "Load balancer DNS: ${ALB_DNS}"

echo "Waiting until both targets are healthy."
aws elbv2 wait target-in-service --target-group-arn "$TG_ARN"
echo "Both targets are healthy."
