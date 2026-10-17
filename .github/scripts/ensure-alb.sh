#!/usr/bin/env bash
# Finds or creates the target group pdm-tg and the internet-facing
# Application Load Balancer pdm-alb, registers both app servers, and waits
# until both targets are healthy. Writes ALB_DNS to GITHUB_ENV.
set -euo pipefail

TG_NAME=pdm-tg
ALB_NAME=pdm-alb

TG_ARN="$(aws elbv2 describe-target-groups --names "$TG_NAME" \
  --query 'TargetGroups[0].TargetGroupArn' --output text 2>/dev/null || true)"
if [ -z "$TG_ARN" ] || [ "$TG_ARN" = "None" ]; then
  TG_ARN="$(aws elbv2 create-target-group --name "$TG_NAME" \
    --protocol HTTP --port 80 --vpc-id "$VPC_ID" --target-type instance \
    --health-check-protocol HTTP --health-check-path /api/admin/setup-status \
    --matcher HttpCode=200 --health-check-interval-seconds 15 \
    --healthy-threshold-count 2 --unhealthy-threshold-count 3 \
    --query 'TargetGroups[0].TargetGroupArn' --output text)"
  echo "Created target group ${TG_NAME}."
else
  echo "Found target group ${TG_NAME}."
fi
aws elbv2 register-targets --target-group-arn "$TG_ARN" \
  --targets "Id=${APP1_ID},Port=80" "Id=${APP2_ID},Port=80"
echo "Registered ${APP1_NAME} (${APP1_ID}) and ${APP2_NAME} (${APP2_ID})."

ALB_ARN="$(aws elbv2 describe-load-balancers --names "$ALB_NAME" \
  --query 'LoadBalancers[0].LoadBalancerArn' --output text 2>/dev/null || true)"
if [ -z "$ALB_ARN" ] || [ "$ALB_ARN" = "None" ]; then
  ALB_ARN="$(aws elbv2 create-load-balancer --name "$ALB_NAME" --type application \
    --scheme internet-facing --ip-address-type ipv4 \
    --subnets "$SUBNET1" "$SUBNET2" --security-groups "${PDM_SG:-$SG_ID}" \
    --query 'LoadBalancers[0].LoadBalancerArn' --output text)"
  echo "Created load balancer ${ALB_NAME}."
else
  echo "Found load balancer ${ALB_NAME}."
fi
aws elbv2 wait load-balancer-available --load-balancer-arns "$ALB_ARN"

listeners="$(aws elbv2 describe-listeners --load-balancer-arn "$ALB_ARN" \
  --query 'length(Listeners[?Port==`80`])' --output text)"
if [ "$listeners" = "0" ]; then
  aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" --protocol HTTP --port 80 \
    --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" >/dev/null
  echo "Created HTTP listener on port 80."
fi

echo "Waiting for both targets to become healthy."
if ! aws elbv2 wait target-in-service --target-group-arn "$TG_ARN" \
    --targets "Id=${APP1_ID},Port=80" "Id=${APP2_ID},Port=80"; then
  aws elbv2 describe-target-health --target-group-arn "$TG_ARN" --output table
  exit 1
fi
aws elbv2 describe-target-health --target-group-arn "$TG_ARN" \
  --query 'TargetHealthDescriptions[].[Target.Id,TargetHealth.State]' --output table

ALB_DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)"
echo "ALB_DNS=${ALB_DNS}" >> "$GITHUB_ENV"
echo "Load balancer http://${ALB_DNS}"
