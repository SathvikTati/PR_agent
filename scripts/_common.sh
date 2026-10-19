# Shared helpers. Sourced by the other scripts; not executable on its own.
set -euo pipefail

STACK_NAME="${STACK_NAME:-pr-notifier}"
AWS_REGION="${AWS_REGION:-${AWS_DEFAULT_REGION:-us-east-1}}"

die() { echo "error: $*" >&2; exit 1; }

require() {
  command -v "$1" >/dev/null 2>&1 || die "$1 is required but not installed"
}

stack_output() {
  local key="$1" value
  value=$(aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" --region "$AWS_REGION" \
    --query "Stacks[0].Outputs[?OutputKey=='${key}'].OutputValue" \
    --output text 2>/dev/null) || die "stack '$STACK_NAME' not found in $AWS_REGION"
  [ -n "$value" ] && [ "$value" != "None" ] || die "stack output '$key' is empty; is the stack deployed?"
  printf '%s' "$value"
}
