# Operations

## Add or change a repository

1. Edit `config/repos.json`.
2. `./scripts/upload-config.sh config/repos.json`

Live within 5 minutes (`CONFIG_CACHE_TTL_SECONDS`). No deploy, no code change,
nothing added to the repository itself. A repository created later in the
organisation is already covered by the org webhook; it only needs its entry in
this file.

The script validates the file with the Lambda's own `parse_config` and refuses
to upload anything the Lambda would skip. The bucket is versioned, so a bad
edit can be rolled back with `aws s3api list-object-versions` and a copy of the
previous version.

## Rotate the webhook signing secret

```bash
./scripts/set-secrets.sh --rotate-webhook-secret
```

Paste the printed value into the GitHub organisation webhook. Deliveries
between the two steps are rejected with `401`; redeliver them from GitHub's
**Recent Deliveries** tab afterwards. Warm containers cache the secret for 15
minutes, so allow for that or recycle them by saving any change to the
function's configuration.

## Rotate the Power Automate URL

Regenerating the flow's trigger URL invalidates the old one. Store the new one:

```bash
./scripts/set-secrets.sh --power-automate-url 'https://...'
```

## Reading the logs

```bash
aws logs tail /aws/lambda/pr-notifier-webhook --follow --since 15m
```

Typical lines: `notified 1 reviewer(s) for CalfusApps/onboarding-services PR
#123 (opened)`, `ignoring closed on ...`, `no reviewers configured for ...`,
`skipping invalid config entry: ...`, `rejected delivery ...: invalid signature`.

The `pr-notifier-function-errors` alarm fires on any Lambda error within a
5-minute window; point it at an SNS topic the team already watches.

## Troubleshooting

**GitHub shows `401 invalid_signature`.**
The secret in GitHub does not match Secrets Manager. Compare with
`./scripts/set-secrets.sh --show-webhook-secret` and re-save the webhook. Check
the webhook's content type is `application/json` — `form-urlencoded` changes
the bytes that were signed.

**GitHub shows `200 not_configured`.**
Working as intended: that repository has no entry. Check the key is the full
`owner/repository` exactly as GitHub reports it.

**GitHub shows `200 ignored_action` for a new PR.**
Either the action is outside the four that notify, or the PR was opened as a
draft — a draft notifies when it is marked *Ready for review*.

**GitHub shows a failed delivery (`502`/`500`).**
The function raised: usually a missing or malformed `config/repos.json`, a
secret that is not set, or Power Automate refusing the POST. The CloudWatch log
has the traceback. Fix the cause and redeliver from GitHub.

**Nothing arrives in Teams but the delivery is `202`.**
The Lambda's work succeeded, so the problem is in the flow. Check its run
history: most often the Flow bot is not a member of the group chat, the
*Post message* step points at the wrong chat, or a `teams_email` is not a user
in the tenant so the mention-token step failed.

**Mentions appear as plain text.**
The flow is interpolating `teams_email` or `github` into the message instead of
the output of the mention-token action. See `powerautomate/README.md` step 3.

**Too many messages.**
`synchronize` fires on every push to a PR branch. Remove it from the `ACTIONS`
set at the top of `lambda_function.py` and redeploy.

## Cost

At 13 repositories this sits inside the AWS free tier in every dimension: a few
thousand Lambda invocations and HTTP API requests a month, one small S3 object,
and two Secrets Manager secrets (~$0.80/month, the only line that is not free).
