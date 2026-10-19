#!/usr/bin/env bash
# Read or set the two secrets. Values are passed as arguments and never
# written to a file in the repository.
#
#   ./scripts/set-secrets.sh --power-automate-url 'https://prod-00...'
#   ./scripts/set-secrets.sh --show-webhook-secret
#   ./scripts/set-secrets.sh --rotate-webhook-secret
source "$(dirname "$0")/_common.sh"
require aws

usage() {
  sed -n '2,8p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-1}"
}

put() {
  aws secretsmanager put-secret-value \
    --secret-id "$1" --secret-string "$2" \
    --region "$AWS_REGION" --output text --query 'VersionId' >/dev/null
}

[ $# -gt 0 ] || usage

case "$1" in
  --power-automate-url)
    [ $# -eq 2 ] || die "--power-automate-url needs exactly one value"
    case "$2" in https://*) ;; *) die "the trigger URL must start with https://" ;; esac
    put "$(stack_output PowerAutomateUrlSecretArn)" "$2"
    echo "Power Automate trigger URL stored."
    ;;
  --github-webhook-secret)
    [ $# -eq 2 ] || die "--github-webhook-secret needs exactly one value"
    put "$(stack_output GitHubWebhookSecretArn)" "$2"
    echo "Webhook signing secret stored. Update the GitHub organisation webhook to match."
    ;;
  --rotate-webhook-secret)
    new=$(LC_ALL=C tr -dc 'A-Za-z0-9' </dev/urandom | head -c 40)
    put "$(stack_output GitHubWebhookSecretArn)" "$new"
    echo "New signing secret generated. Paste this into the GitHub webhook now:"
    echo "$new"
    echo "(Deliveries fail until GitHub is updated, and can be redelivered afterwards.)"
    ;;
  --show-webhook-secret)
    aws secretsmanager get-secret-value \
      --secret-id "$(stack_output GitHubWebhookSecretArn)" \
      --region "$AWS_REGION" --query SecretString --output text
    ;;
  -h|--help) usage 0 ;;
  *) die "unknown option: $1" ;;
esac
