"""Provider limits need actual response evidence before a resume decision exists."""

from __future__ import annotations

from datetime import UTC, datetime

from daedalus.providers.failure_evidence import failure_evidence
from daedalus.providers.openai_compat import classify_failure

OBSERVED = datetime(2026, 10, 4, 12, tzinfo=UTC)


def test_auth_status_overrides_misleading_rate_body() -> None:
    body = '{"error":{"type":"rate_limit_error","code":"rate_limit_exceeded"}}'
    verdict = classify_failure(401, body)
    evidence = failure_evidence(401, body, {"Retry-After": "60", "x-ratelimit-reset-requests": "1m"},
                                provider_kind="openai_compat", provider_host="api.openai.com",
                                observed_at=OBSERVED)
    assert (verdict.reason, verdict.retryable) == ("auth", False)
    assert evidence.failure_class == "auth"
    assert evidence.reset_at is None
    assert evidence.retry_after_at == "2026-10-04T12:01:00+00:00"


def test_bare_429_is_unknown_even_with_retry_suggestion() -> None:
    evidence = failure_evidence(429, "slow down", {"Retry-After": "30",
                                                     "x-ratelimit-reset-requests": "1m"},
                                provider_kind="openai_compat", provider_host="api.openai.com",
                                observed_at=OBSERVED)
    assert evidence.failure_class == "limited_unknown"
    assert evidence.reset_at is None
    assert evidence.retry_after_at == "2026-10-04T12:00:30+00:00"


def test_explicit_provider_rate_reset_is_bounded_and_origin_pinned() -> None:
    body = '{"error":{"code":"rate_limit_exceeded"}}'
    headers = {"x-ratelimit-reset-requests": "1m", "x-ratelimit-reset-tokens": "2m"}
    official = failure_evidence(429, body, headers, provider_kind="openai_compat",
                                provider_host="api.openai.com", observed_at=OBSERVED)
    proxy = failure_evidence(429, body, headers, provider_kind="openai_compat",
                             provider_host="model.example", observed_at=OBSERVED)
    assert official.failure_class == "rate"
    assert official.reset_at == "2026-10-04T12:02:00+00:00"
    assert official.reset_source == "x-ratelimit-reset-tokens"
    assert proxy.reset_at is None


def test_quota_code_never_claims_a_rate_reset() -> None:
    body = '{"error":{"code":"insufficient_quota"}}'
    assert classify_failure(429, body).reason == "billing"
    evidence = failure_evidence(429, body, {"x-ratelimit-reset-requests": "30s"},
                                provider_kind="openai_compat", provider_host="api.openai.com",
                                observed_at=OBSERVED)
    assert (evidence.failure_class, evidence.reset_at) == ("quota", None)


def test_bad_or_long_reset_does_not_create_a_timer() -> None:
    body = '{"error":{"code":"rate_limit_exceeded"}}'
    for value in ("", "-1", "1000h", "tomorrow", "x" * 1000):
        evidence = failure_evidence(429, body, {"x-ratelimit-reset-requests": value},
                                    provider_kind="openai_compat", provider_host="api.openai.com",
                                    observed_at=OBSERVED)
        assert evidence.reset_at is None


GO_USAGE_LIMIT = ('{"type":"error","error":{"type":"GoUsageLimitError","message":"Go usage limit exceeded"},'
                  '"metadata":{"workspace":"wrk_1","limitName":"monthly"}}')


def test_an_exhausted_opencode_allowance_is_a_quota_and_not_retried_in_place() -> None:
    evidence = failure_evidence(429, GO_USAGE_LIMIT, {"Retry-After": "93600"}, provider_kind="opencode",
                                observed_at=OBSERVED)
    assert (evidence.failure_class, evidence.provider_code) == ("quota", "gousagelimiterror")
    assert evidence.reset_at is None
    assert evidence.retry_after_at is not None
    verdict = classify_failure(429, GO_USAGE_LIMIT)
    assert (verdict.reason, verdict.retryable) == ("billing", False)
    assert classify_failure(429, '{"error":{"type":"rate_limit_error"}}').retryable is True
