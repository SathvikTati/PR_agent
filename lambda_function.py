"""GitHub pull-request webhook -> Power Automate -> Teams group chat.

One organisation webhook reaches this function for every repository. It
verifies GitHub's signature, keeps only the pull-request actions worth
reporting, looks up the repository's reviewers in a JSON file in S3, and
posts a flat payload to Power Automate, which does the Teams @mentions.
"""

import base64
import hashlib
import hmac
import json
import logging
import os
import time
import urllib.request

import boto3

log = logging.getLogger()
log.setLevel(os.environ.get("LOG_LEVEL", "INFO"))

ACTIONS = {"opened", "reopened", "ready_for_review", "synchronize"}

s3 = boto3.client("s3")
secretsmanager = boto3.client("secretsmanager")

# Warm-container cache: key -> (expires_at, value).
_cache = {}


def cached(key, ttl, load):
    """Return a cached value, reloading it once the TTL has passed."""
    hit = _cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    value = load()
    _cache[key] = (time.time() + ttl, value)
    return value


def get_secret(secret_id):
    """Secret string from Secrets Manager, cached for 15 minutes."""
    def load():
        return secretsmanager.get_secret_value(SecretId=secret_id)["SecretString"].strip()
    return cached(secret_id, 900, load)


def parse_config(document):
    """Validate the config document into {repo_lowercase: [reviewers]}.

    A bad entry is skipped and logged so one typo cannot silence the rest.
    """
    repos = {}
    for name, entry in document.items():
        reviewers = (entry or {}).get("reviewers") if isinstance(entry, dict) else None
        reviewers = [
            r for r in reviewers or []
            if isinstance(r, dict) and r.get("github") and "@" in str(r.get("teams_email", ""))
        ]
        if "/" not in name or not reviewers:
            log.warning("skipping invalid config entry: %s", name)
            continue
        repos[name.lower()] = reviewers
    return repos


def get_config():
    """Repository configuration from S3.

    Cached for CONFIG_CACHE_TTL_SECONDS (default 300), so an edit in S3 takes
    effect within five minutes without a redeploy.
    """
    def load():
        bucket = os.environ["CONFIG_BUCKET"]
        key = os.environ.get("CONFIG_KEY", "config/repos.json")
        body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
        return parse_config(json.loads(body))
    return cached("config", int(os.environ.get("CONFIG_CACHE_TTL_SECONDS", "300")), load)


def verify_signature(body, signature, secret):
    """True if GitHub's X-Hub-Signature-256 matches the raw request body."""
    expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return bool(signature) and hmac.compare_digest(expected, signature)


def build_payload(event, delivery_id, reviewers):
    """Flatten the GitHub event. Branches come from the payload, unfiltered."""
    pr = event.get("pull_request") or {}
    return {
        "delivery_id": delivery_id,
        "repository": (event.get("repository") or {}).get("full_name", ""),
        "action": event.get("action", ""),
        "number": pr.get("number"),
        "title": pr.get("title", ""),
        "author": (pr.get("user") or {}).get("login", ""),
        "source_branch": (pr.get("head") or {}).get("ref", ""),
        "target_branch": (pr.get("base") or {}).get("ref", ""),
        "description": (pr.get("body") or "")[:1500],
        "url": pr.get("html_url", ""),
        "reviewers": reviewers,
    }


def post_to_power_automate(url, payload):
    """POST the payload to the flow's HTTP trigger. The URL is a secret."""
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=10) as response:
        return response.status


def reply(status, outcome):
    return {"statusCode": status, "body": json.dumps({"outcome": outcome})}


def lambda_handler(event, context=None):
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    body = event.get("body") or ""
    raw = base64.b64decode(body) if event.get("isBase64Encoded") else body.encode()
    delivery_id = headers.get("x-github-delivery", "")

    # Check the signature before trusting anything in the body.
    secret = get_secret(os.environ["GITHUB_WEBHOOK_SECRET_ID"])
    if not verify_signature(raw, headers.get("x-hub-signature-256", ""), secret):
        log.warning("rejected delivery %s: invalid signature", delivery_id)
        return reply(401, "invalid_signature")

    if headers.get("x-github-event") != "pull_request":
        return reply(200, "ignored_event")

    github_event = json.loads(raw)
    action = github_event.get("action", "")
    pr = github_event.get("pull_request") or {}
    repo = (github_event.get("repository") or {}).get("full_name", "")

    # A draft stays quiet until it is marked ready_for_review.
    draft = pr.get("draft") and action != "ready_for_review"
    if action not in ACTIONS or draft:
        log.info("ignoring %s on %s", action, repo)
        return reply(200, "ignored_action")

    reviewers = get_config().get(repo.lower())
    if not reviewers:
        log.info("no reviewers configured for %s", repo)
        return reply(200, "not_configured")

    url = get_secret(os.environ["POWER_AUTOMATE_URL_SECRET_ID"])
    post_to_power_automate(url, build_payload(github_event, delivery_id, reviewers))

    log.info("notified %s reviewer(s) for %s PR #%s (%s)",
             len(reviewers), repo, pr.get("number"), action)
    return reply(202, "notified")
