# PR Agent — centralised GitHub → Teams pull-request notifier

One organisation-level GitHub webhook notifies a Microsoft Teams group chat
about pull requests across every repository in the organisation, mentioning the
reviewers configured for that repository.

No GitHub Actions workflow and no application code live in the 13 (or 130)
repositories being watched. Adding a repository is an edit to one JSON file in
S3.

```
GitHub org webhook ──POST──▶ API Gateway HTTP API ──▶ Lambda (Python 3.12)
                                                        │
                        ┌───────────────────────────────┤
                        ▼                               ▼
              S3  config/repos.json          Secrets Manager
              (repo → reviewers)        (signing secret, flow URL)
                        │
                        ▼
              Power Automate HTTP trigger ──▶ Teams group chat
                                               (@mention tokens)
```

The Lambda verifies the webhook signature, filters the pull-request actions
worth reporting, resolves the repository's reviewers from S3, and posts a flat
JSON payload to Power Automate. Power Automate turns each reviewer email into a
genuine Teams mention token and posts the message into the existing group chat.

## Layout

| Path | Purpose |
| --- | --- |
| `lambda_function.py` | The whole service, ~150 lines: signature check, filtering, config, delivery. |
| `template.yaml` | SAM template: HTTP API, Lambda, S3 bucket, secrets, IAM, alarm. |
| `config/repos.json` | The repository → reviewer map that lives in S3. |
| `config/repos.schema.json` | JSON Schema for that file, for editors and CI. |
| `powerautomate/` | Flow build guide, trigger schema, sample payload. |
| `scripts/` | Deploy, set secrets, validate-and-upload config. |
| `tests/` | Test suite; no AWS calls, no network. |
| `docs/operations.md` | Runbook: troubleshooting, rotation, adding repositories. |

Runtime dependencies: the Python 3.12 Lambda runtime and boto3, which the
runtime already ships. Nothing to vendor or package.

## Deploy

Prerequisites: AWS CLI and AWS SAM CLI, credentials for the target account, and
permission to create a GitHub organisation webhook.

### 1. Create the flow and get its trigger URL

Follow [`powerautomate/README.md`](powerautomate/README.md). Stop after saving
the trigger — you need its **HTTP POST URL**.

### 2. Deploy the stack

```bash
STACK_NAME=pr-notifier AWS_REGION=eu-west-1 ./scripts/deploy.sh
```

The outputs include the **webhook payload URL** and the config bucket name. The
stack generates a strong webhook signing secret, so no secret material ever
appears in the template or in git.

### 3. Store the Power Automate URL

```bash
./scripts/set-secrets.sh --power-automate-url 'https://prod-00.westeurope.logic.azure.com:443/workflows/...'
```

### 4. Upload the repository configuration

Edit `config/repos.json`, then:

```bash
./scripts/upload-config.sh config/repos.json
```

The script validates the file with the same `parse_config` the Lambda uses and
refuses to upload anything the Lambda would skip.

### 5. Create the GitHub organisation webhook

GitHub → your organisation → **Settings → Webhooks → Add webhook**:

| Field | Value |
| --- | --- |
| Payload URL | the `WebhookUrl` stack output |
| Content type | `application/json` |
| Secret | `./scripts/set-secrets.sh --show-webhook-secret` |
| SSL verification | Enable |
| Events | *Let me select individual events* → **Pull requests** only |
| Active | ✔ |

One webhook at the organisation level covers every repository, including ones
created later. Nothing is installed per repository.

### 6. Verify

Open a pull request in a configured repository and watch the group chat. The
delivery in GitHub's **Recent Deliveries** tab should read
`202 {"outcome":"notified"}`.

## Configuration

`config/repos.json` in S3, keyed by the full `owner/repository` name:

```json
{
  "CalfusApps/onboarding-services": {
    "reviewers": [
      { "github": "sathvik", "teams_email": "sathvik.tati@calfus.com" }
    ]
  },
  "mvue-ai/mvue-ai-connectors": {
    "reviewers": [
      { "github": "reviewer2", "teams_email": "reviewer2@calfus.com" },
      { "github": "reviewer3", "teams_email": "reviewer3@calfus.com" }
    ]
  }
}
```

- Any number of reviewers per repository; different reviewers per repository.
- Repository names and reviewers are never hardcoded in the Lambda.
- Keys are matched case-insensitively, as GitHub treats them.
- A repository that is not listed is ignored and logged — never an error.
- A malformed entry is skipped and logged; the other repositories keep working.
- The file is read from S3 and cached for `CONFIG_CACHE_TTL_SECONDS`
  (**default 300s**). That is the documented refresh interval: an upload takes
  effect within five minutes with no redeploy.
- Only data is ever read — the Lambda never loads code from a repository or a
  pull-request branch.

## Behaviour

| Event | Result | HTTP |
| --- | --- | --- |
| `opened`, `reopened`, `ready_for_review`, `synchronize` on a configured repo | Teams message sent | `202` |
| Any other action (`closed`, `labeled`, `edited`, …) | Ignored, no message | `200` |
| Draft pull request | Ignored until `ready_for_review` | `200` |
| Repository not in `repos.json` | Ignored, logged | `200` |
| Any non-`pull_request` event, including `ping` | Ignored | `200` |
| Invalid or missing signature | Rejected before the body is parsed | `401` |
| Config, secret, or Power Automate failure | Raises; GitHub records a failed delivery and it can be redelivered | `5xx` |

Branches are never filtered. `feature/sso → develop`, `bugfix/login →
release-4.2.5` and `st/connectors_fixs → main` all notify, because the source
and target come straight from the event payload.

To change which actions notify, edit the `ACTIONS` set at the top of
`lambda_function.py`.

## Notification payload

The Lambda posts exactly these fields to Power Automate:

```json
{
  "delivery_id": "4f2a9c10-7b3d-11ef-9c21-3f1a2b4c5d6e",
  "repository": "CalfusApps/onboarding-services",
  "action": "opened",
  "number": 123,
  "title": "Add SSO authentication",
  "author": "sathvik",
  "source_branch": "feature/sso",
  "target_branch": "develop",
  "description": "Adds SSO authentication.",
  "url": "https://github.com/CalfusApps/onboarding-services/pull/123",
  "reviewers": [{ "github": "sathvik", "teams_email": "sathvik.tati@calfus.com" }]
}
```

`delivery_id` is GitHub's `X-GitHub-Delivery` header, so a message in Teams can
be traced back to a delivery in GitHub and to a log line in CloudWatch.

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CONFIG_BUCKET` | *(required)* | Bucket holding the configuration. |
| `CONFIG_KEY` | `config/repos.json` | Key of the configuration object. |
| `CONFIG_CACHE_TTL_SECONDS` | `300` | Config refresh interval. |
| `GITHUB_WEBHOOK_SECRET_ID` | *(required)* | Secret ARN/name of the signing secret. |
| `POWER_AUTOMATE_URL_SECRET_ID` | *(required)* | Secret ARN/name of the flow URL. |
| `LOG_LEVEL` | `INFO` | Log verbosity. |

All five are set by the template; none holds a secret value.

## Security

- Every request is rejected unless its `X-Hub-Signature-256` HMAC matches the
  raw body, compared with `hmac.compare_digest` **before** the body is parsed.
- Secrets live only in Secrets Manager. The template generates them; none is
  committed, logged, or passed through the environment as a value.
- The Lambda's IAM policy allows exactly `s3:GetObject` on the single config
  key and `secretsmanager:GetSecretValue` on the two secrets.
- The bucket blocks all public access, enforces bucket-owner ownership,
  encrypts objects at rest, is versioned, and denies non-TLS requests.

## Tests

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

44 tests, no AWS calls and no network: the boto3 clients and `urlopen` are
replaced with fakes. They cover signature verification, config parsing and
caching, payload construction, and every handler branch in the table above.

## Operating it

See [`docs/operations.md`](docs/operations.md) for adding a repository,
rotating the signing secret, reading the logs, and the common failure modes.
