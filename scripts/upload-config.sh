#!/usr/bin/env bash
# Validate repos.json with the same code the Lambda uses, then upload it.
#
#   ./scripts/upload-config.sh config/repos.json
#
# A change is live within CONFIG_CACHE_TTL_SECONDS (300s by default) with no
# redeploy.
source "$(dirname "$0")/_common.sh"
require aws

root="$(cd "$(dirname "$0")/.." && pwd)"
file="${1:-$root/config/repos.json}"
[ -f "$file" ] || die "no such file: $file"

python3 - "$root" "$file" <<'PYEOF'
import json, sys, importlib.util
root, path = sys.argv[1], sys.argv[2]
spec = importlib.util.spec_from_file_location("lf", f"{root}/lambda_function.py")
lf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lf)

with open(path, encoding="utf-8") as handle:
    document = json.load(handle)

repos = lf.parse_config(document)
skipped = [name for name in document if name.lower() not in repos]
for name in skipped:
    print(f"  invalid entry, would be skipped: {name}", file=sys.stderr)
if skipped:
    sys.exit("refusing to upload: fix the entries above first")
if not repos:
    sys.exit("refusing to upload: no valid repositories in the file")
print(f"  valid: {len(repos)} repositories, "
      f"{sum(len(r) for r in repos.values())} reviewer entries")
PYEOF

bucket="$(stack_output ConfigBucketName)"
key="${CONFIG_KEY:-config/repos.json}"
aws s3 cp "$file" "s3://${bucket}/${key}" --region "$AWS_REGION" >/dev/null
echo "Uploaded to s3://${bucket}/${key} (live within the cache TTL, 300s by default)."
