#!/usr/bin/env bash
# Build and deploy the stack.
#
#   STACK_NAME=pr-notifier AWS_REGION=eu-west-1 ./scripts/deploy.sh
#
# Pass --guided on the first run to be prompted for parameters.
source "$(dirname "$0")/_common.sh"
require aws
require sam

cd "$(dirname "$0")/.."

sam build --template template.yaml

sam deploy \
  --stack-name "$STACK_NAME" \
  --region "$AWS_REGION" \
  --capabilities CAPABILITY_IAM \
  --no-fail-on-empty-changeset \
  --resolve-s3 \
  "$@"

echo
echo "Webhook payload URL : $(stack_output WebhookUrl)"
echo "Config object       : $(stack_output ConfigObjectUri)"
echo
echo "Next: ./scripts/set-secrets.sh --power-automate-url '<flow url>'"
echo "      ./scripts/upload-config.sh config/repos.json"
echo "      ./scripts/set-secrets.sh --show-webhook-secret   # paste into GitHub"
