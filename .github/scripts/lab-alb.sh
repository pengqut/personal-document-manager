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
LISTENER_ARN="$(aws elbv2 create-listener --load-balancer-arn "$ALB_ARN" \
  --protocol HTTP --port 80 \
  --default-actions "Type=forward,TargetGroupArn=${TG_ARN}" \
  --query 'Listeners[0].ListenerArn' --output text)"
note LISTENER_ARN "$LISTENER_ARN"

aws elbv2 wait load-balancer-available --load-balancer-arns "$ALB_ARN"
ALB_DNS="$(aws elbv2 describe-load-balancers --load-balancer-arns "$ALB_ARN" \
  --query 'LoadBalancers[0].DNSName' --output text)"
note ALB_DNS "$ALB_DNS"
echo "Load balancer DNS: ${ALB_DNS}"

echo "Waiting until both targets are healthy."
aws elbv2 wait target-in-service --target-group-arn "$TG_ARN"
echo "Both targets are healthy."

# Amplify would call the load balancer from the public internet, and the
# student role cannot open that path. API Gateway connects through a VPC
# link that uses the same security group as the load balancer, which
# already allows that traffic, and publishes an HTTPS address.
VPC_LINK_ID="$(aws apigatewayv2 create-vpc-link --name "pdm-link-${RUN_ID}" \
  --subnet-ids "$SUBNET1" "$SUBNET2" --security-group-ids "$ALB_SG" \
  --query VpcLinkId --output text)"
note VPC_LINK_ID "$VPC_LINK_ID"
echo "Waiting for the VPC link ${VPC_LINK_ID}."
link_ready=0
for try in $(seq 1 40); do
  link_status="$(aws apigatewayv2 get-vpc-link --vpc-link-id "$VPC_LINK_ID" --query VpcLinkStatus --output text)"
  echo "VPC link status: ${link_status}"
  if [ "$link_status" = "AVAILABLE" ]; then
    link_ready=1
    break
  fi
  if [ "$link_status" = "FAILED" ]; then
    echo "The VPC link failed." >&2
    exit 1
  fi
  sleep 15
done
if [ "$link_ready" -ne 1 ]; then
  echo "The VPC link did not become available." >&2
  exit 1
fi

API_ID="$(aws apigatewayv2 create-api --name "pdm-${RUN_ID}" --protocol-type HTTP \
  --query ApiId --output text)"
note API_ID "$API_ID"
INTEGRATION_ID="$(aws apigatewayv2 create-integration --api-id "$API_ID" \
  --integration-type HTTP_PROXY --integration-method ANY \
  --connection-type VPC_LINK --connection-id "$VPC_LINK_ID" \
  --integration-uri "$LISTENER_ARN" --payload-format-version 1.0 \
  --query IntegrationId --output text)"
note INTEGRATION_ID "$INTEGRATION_ID"
aws apigatewayv2 create-route --api-id "$API_ID" --route-key '$default' \
  --target "integrations/${INTEGRATION_ID}" >/dev/null
aws apigatewayv2 create-stage --api-id "$API_ID" --stage-name '$default' --auto-deploy >/dev/null
PUBLIC_URL="$(aws apigatewayv2 get-api --api-id "$API_ID" --query ApiEndpoint --output text)"
note PUBLIC_URL "$PUBLIC_URL"
echo "Public application URL: ${PUBLIC_URL}"

public_ready=0
for try in $(seq 1 12); do
  if curl -fsS -D - "${PUBLIC_URL}/api/admin/setup-status" -o /tmp/public-body.json; then
    public_ready=1
    break
  fi
  sleep 10
done
if [ "$public_ready" -ne 1 ]; then
  echo "The public URL did not respond." >&2
  exit 1
fi
