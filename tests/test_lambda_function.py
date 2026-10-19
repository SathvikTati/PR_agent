"""Tests for the PR notifier Lambda. No AWS calls, no network."""

import base64
import io
import json
import os
import sys

import pytest

os.environ.setdefault("AWS_DEFAULT_REGION", "us-east-1")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lambda_function as lf  # noqa: E402

SECRET = "webhook-signing-secret"
FLOW_URL = "https://prod-00.example.logic.azure.com/workflows/abc/triggers/manual/invoke?sig=zzz"
GITHUB_SECRET_ID = "pr-notifier/github-webhook-secret"
FLOW_SECRET_ID = "pr-notifier/power-automate-url"

CONFIG = {
    "CalfusApps/onboarding-services": {
        "reviewers": [{"github": "sathvik", "teams_email": "sathvik.tati@calfus.com"}]
    },
    "mvue-ai/mvue-ai-connectors": {
        "reviewers": [
            {"github": "reviewer2", "teams_email": "reviewer2@calfus.com"},
            {"github": "reviewer3", "teams_email": "reviewer3@calfus.com"},
        ]
    },
}


class FakeS3:
    def __init__(self, document):
        self.document = document
        self.calls = 0

    def get_object(self, Bucket, Key):  # noqa: N803 - boto3 casing
        self.calls += 1
        body = self.document
        if not isinstance(body, bytes):
            body = json.dumps(body).encode()
        return {"Body": io.BytesIO(body)}


class FakeSecrets:
    def __init__(self):
        self.calls = 0

    def get_secret_value(self, SecretId):  # noqa: N803 - boto3 casing
        self.calls += 1
        return {"SecretString": SECRET if SecretId == GITHUB_SECRET_ID else FLOW_URL}


class FakeResponse:
    status = 202

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def aws(monkeypatch):
    """Replace the AWS clients and clear the warm-container cache."""
    monkeypatch.setenv("CONFIG_BUCKET", "pr-notifier-config")
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET_ID", GITHUB_SECRET_ID)
    monkeypatch.setenv("POWER_AUTOMATE_URL_SECRET_ID", FLOW_SECRET_ID)
    monkeypatch.setattr(lf, "s3", FakeS3(CONFIG))
    monkeypatch.setattr(lf, "secretsmanager", FakeSecrets())
    lf._cache.clear()
    yield
    lf._cache.clear()


@pytest.fixture
def sent(monkeypatch):
    """Capture what would be posted to Power Automate."""
    captured = []

    def fake_urlopen(request, timeout=None):
        captured.append({"url": request.full_url, "payload": json.loads(request.data)})
        return FakeResponse()

    monkeypatch.setattr(lf.urllib.request, "urlopen", fake_urlopen)
    return captured


def pr_event(action="opened", repo="CalfusApps/onboarding-services",
             source="feature/sso", target="develop", draft=False, **pr):
    """A GitHub pull_request webhook body."""
    return {
        "action": action,
        "repository": {"full_name": repo},
        "pull_request": {
            "number": 123,
            "title": "Add SSO authentication",
            "body": "Adds SSO authentication.",
            "html_url": f"https://github.com/{repo}/pull/123",
            "draft": draft,
            "user": {"login": "sathvik"},
            "head": {"ref": source},
            "base": {"ref": target},
            **pr,
        },
    }


def request(body, secret=SECRET, event_type="pull_request", signature=None, b64=False):
    """Wrap a webhook body in an API Gateway request."""
    raw = body if isinstance(body, bytes) else json.dumps(body).encode()
    headers = {"X-GitHub-Event": event_type, "X-GitHub-Delivery": "delivery-1"}
    if signature is None:
        signature = "sha256=" + lf.hmac.new(secret.encode(), raw, lf.hashlib.sha256).hexdigest()
    if signature is not False:
        headers["X-Hub-Signature-256"] = signature
    return {
        "headers": headers,
        "isBase64Encoded": b64,
        "body": base64.b64encode(raw).decode() if b64 else raw.decode(),
    }


def outcome(response):
    return json.loads(response["body"])["outcome"]


# --- signature ------------------------------------------------------------

class TestSignature:
    def test_accepts_a_matching_signature(self):
        body = b'{"action":"opened"}'
        signature = "sha256=" + lf.hmac.new(SECRET.encode(), body, lf.hashlib.sha256).hexdigest()
        assert lf.verify_signature(body, signature, SECRET)

    def test_rejects_a_signature_for_different_bytes(self):
        signature = "sha256=" + lf.hmac.new(SECRET.encode(), b"a", lf.hashlib.sha256).hexdigest()
        assert not lf.verify_signature(b"b", signature, SECRET)

    def test_rejects_another_secret(self):
        body = b"{}"
        signature = "sha256=" + lf.hmac.new(b"wrong", body, lf.hashlib.sha256).hexdigest()
        assert not lf.verify_signature(body, signature, SECRET)

    @pytest.mark.parametrize("signature", ["", "sha1=abc", "deadbeef"])
    def test_rejects_missing_or_malformed_signatures(self, signature):
        assert not lf.verify_signature(b"{}", signature, SECRET)


# --- configuration --------------------------------------------------------

class TestConfig:
    def test_reads_repositories_and_reviewers(self, sent):
        config = lf.get_config()
        assert config["calfusapps/onboarding-services"][0]["github"] == "sathvik"
        assert len(config["mvue-ai/mvue-ai-connectors"]) == 2

    def test_warm_invocations_reuse_the_cache(self):
        lf.get_config()
        lf.get_config()
        assert lf.s3.calls == 1

    def test_the_cache_expires(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr(lf.time, "time", lambda: now[0])
        lf.get_config()
        now[0] += 301
        lf.get_config()
        assert lf.s3.calls == 2

    @pytest.mark.parametrize("entry", [
        {"reviewers": []},
        {},
        {"reviewers": [{"github": "x"}]},
        {"reviewers": [{"teams_email": "x@y.com"}]},
        {"reviewers": [{"github": "x", "teams_email": "not-an-email"}]},
        "not-an-object",
    ])
    def test_invalid_entries_are_skipped(self, entry):
        assert lf.parse_config({"owner/repo": entry}) == {}

    def test_a_bad_entry_does_not_silence_the_others(self):
        config = lf.parse_config({
            "owner/good": {"reviewers": [{"github": "a", "teams_email": "a@b.com"}]},
            "owner/broken": {"reviewers": []},
        })
        assert list(config) == ["owner/good"]

    def test_a_key_that_is_not_owner_slash_repo_is_skipped(self):
        assert lf.parse_config({"onboarding-services": CONFIG["CalfusApps/onboarding-services"]}) == {}


class TestSecrets:
    def test_is_cached_between_invocations(self):
        lf.get_secret(GITHUB_SECRET_ID)
        lf.get_secret(GITHUB_SECRET_ID)
        assert lf.secretsmanager.calls == 1


# --- payload --------------------------------------------------------------

class TestPayload:
    def test_contains_every_documented_field(self):
        payload = lf.build_payload(pr_event(), "d-1", [])
        assert set(payload) == {
            "delivery_id", "repository", "action", "number", "title", "author",
            "source_branch", "target_branch", "description", "url", "reviewers",
        }

    def test_maps_the_event_onto_the_payload(self):
        reviewers = [{"github": "a", "teams_email": "a@b.com"}]
        payload = lf.build_payload(pr_event(), "d-1", reviewers)
        assert payload["repository"] == "CalfusApps/onboarding-services"
        assert payload["action"] == "opened"
        assert payload["number"] == 123
        assert payload["title"] == "Add SSO authentication"
        assert payload["author"] == "sathvik"
        assert payload["description"] == "Adds SSO authentication."
        assert payload["url"].endswith("/pull/123")
        assert payload["delivery_id"] == "d-1"
        assert payload["reviewers"] == reviewers

    def test_a_missing_description_becomes_an_empty_string(self):
        assert lf.build_payload(pr_event(body=None), "d-1", [])["description"] == ""

    def test_a_long_description_is_truncated(self):
        assert len(lf.build_payload(pr_event(body="x" * 5000), "d-1", [])["description"]) == 1500

    def test_a_sparse_event_does_not_raise(self):
        assert lf.build_payload({"action": "opened"}, "d-1", [])["number"] is None


# --- handler --------------------------------------------------------------

class TestHandler:
    def test_a_configured_repository_is_notified(self, sent):
        response = lf.lambda_handler(request(pr_event()))
        assert response["statusCode"] == 202
        assert outcome(response) == "notified"
        assert sent[0]["url"] == FLOW_URL
        assert sent[0]["payload"]["reviewers"] == CONFIG["CalfusApps/onboarding-services"]["reviewers"]
        assert sent[0]["payload"]["delivery_id"] == "delivery-1"

    def test_a_base64_encoded_request_is_handled(self, sent):
        assert lf.lambda_handler(request(pr_event(), b64=True))["statusCode"] == 202

    @pytest.mark.parametrize("action", ["opened", "reopened", "ready_for_review", "synchronize"])
    def test_every_supported_action_notifies(self, action, sent):
        assert lf.lambda_handler(request(pr_event(action=action)))["statusCode"] == 202
        assert sent[-1]["payload"]["action"] == action

    @pytest.mark.parametrize("action", ["closed", "labeled", "edited", "review_requested"])
    def test_unsupported_actions_are_ignored(self, action, sent):
        response = lf.lambda_handler(request(pr_event(action=action)))
        assert response["statusCode"] == 200
        assert outcome(response) == "ignored_action"
        assert sent == []

    @pytest.mark.parametrize("source,target", [
        ("feature/sso", "develop"),
        ("bugfix/login", "release-4.2.5"),
        ("st/connectors_fixs", "main"),
    ])
    def test_no_branch_combination_is_filtered_out(self, source, target, sent):
        assert lf.lambda_handler(request(pr_event(source=source, target=target)))["statusCode"] == 202
        assert sent[-1]["payload"]["source_branch"] == source
        assert sent[-1]["payload"]["target_branch"] == target

    def test_an_unknown_repository_is_ignored(self, sent):
        response = lf.lambda_handler(request(pr_event(repo="CalfusApps/other")))
        assert response["statusCode"] == 200
        assert outcome(response) == "not_configured"
        assert sent == []

    def test_repository_lookup_is_case_insensitive(self, sent):
        assert lf.lambda_handler(request(pr_event(repo="calfusapps/Onboarding-Services")))["statusCode"] == 202

    def test_a_draft_stays_quiet_until_ready_for_review(self, sent):
        assert outcome(lf.lambda_handler(request(pr_event(draft=True)))) == "ignored_action"
        assert sent == []
        ready = request(pr_event(action="ready_for_review", draft=True))
        assert lf.lambda_handler(ready)["statusCode"] == 202

    def test_a_bad_signature_is_rejected_before_anything_else(self, sent):
        response = lf.lambda_handler(request(pr_event(), secret="attacker-secret"))
        assert response["statusCode"] == 401
        assert sent == []
        assert lf.s3.calls == 0

    def test_a_missing_signature_is_rejected(self, sent):
        assert lf.lambda_handler(request(pr_event(), signature=False))["statusCode"] == 401
        assert sent == []

    def test_a_tampered_body_is_rejected(self, sent):
        event = request(pr_event())
        event["body"] = event["body"].replace("feature/sso", "evil/branch")
        assert lf.lambda_handler(event)["statusCode"] == 401
        assert sent == []

    def test_other_github_events_are_ignored(self, sent):
        response = lf.lambda_handler(request({"zen": "Keep it logically awesome."}, event_type="ping"))
        assert outcome(response) == "ignored_event"
        assert sent == []

    def test_multiple_reviewers_are_all_forwarded(self, sent):
        assert lf.lambda_handler(request(pr_event(repo="mvue-ai/mvue-ai-connectors")))["statusCode"] == 202
        assert len(sent[0]["payload"]["reviewers"]) == 2
