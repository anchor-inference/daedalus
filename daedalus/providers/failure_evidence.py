"""Bounded facts from a provider refusal, without retaining response secrets."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.utils import parsedate_to_datetime

_DURATION = re.compile(r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$")
_CODES = {
    "insufficient_quota": "quota",
    "billing_hard_limit_reached": "quota",
    "billing_not_active": "billing",
    "rate_limit_exceeded": "rate",
    "rate_limit_error": "rate",
    "requests_limit_exceeded": "rate",
    "tokens_limit_exceeded": "rate",
    "overloaded_error": "capacity",
    "capacity_exceeded": "capacity",
}


@dataclass(frozen=True, slots=True)
class FailureEvidence:
    status: int
    failure_class: str
    provider_code: str | None
    reset_at: str | None
    reset_source: str | None
    retry_after_at: str | None
    digest: str


def _future(value: str, observed: datetime, *, duration: bool = False) -> str | None:
    if len(value) > 96:
        return None
    try:
        if duration:
            match = _DURATION.fullmatch(value.strip())
            if match is None or all(group is None for group in match.groups()):
                return None
            hours, minutes, seconds = match.groups()
            instant = observed + timedelta(hours=int(hours or 0), minutes=int(minutes or 0),
                                           seconds=float(seconds or 0))
        else:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is None:
                return None
            instant = instant.astimezone(UTC)
    except (OverflowError, ValueError):
        return None
    if instant <= observed or instant > observed + timedelta(days=7):
        return None
    return instant.isoformat()


def _retry_after(value: str, observed: datetime) -> str | None:
    if len(value) > 96:
        return None
    try:
        seconds = int(value)
        instant = observed + timedelta(seconds=seconds)
    except ValueError:
        try:
            instant = parsedate_to_datetime(value).astimezone(UTC)
        except (OverflowError, TypeError, ValueError):
            return None
    return instant.isoformat() if observed < instant <= observed + timedelta(days=7) else None


def failure_evidence(status: int, text: str, headers: Mapping[str, str], *,
                     provider_kind: str, provider_host: str = "",
                     observed_at: datetime | None = None) -> FailureEvidence:
    """A suggested retry is distinct from a provider-declared capacity reset.

    Generic OpenAI-compatible endpoints can emit arbitrary headers. Only named vendor kinds
    receive a reset interpretation, and an unrelated 429 never becomes a quota claim.
    """
    observed = (observed_at or datetime.now(UTC)).astimezone(UTC)
    safe_headers = {str(key).lower(): str(value)[:96] for key, value in headers.items()
                    if str(key).lower() in {"retry-after", "x-ratelimit-reset-requests",
                                             "x-ratelimit-reset-tokens", "x-ratelimit-reset"}}
    try:
        data = json.loads(text)
    except ValueError:
        data = {}
    error = data.get("error", data) if isinstance(data, dict) else {}
    error = error if isinstance(error, dict) else {}
    code = str(error.get("code") or error.get("type") or "").lower()[:80]
    if status in (401, 403):
        failure_class = "auth"
    elif status == 402:
        failure_class = "billing"
    elif status in (429, 503, 529):
        failure_class = _CODES.get(code, "limited_unknown" if status == 429 else "capacity_unknown")
    else:
        failure_class = _CODES.get(code, "other")
    retry_at = _retry_after(safe_headers["retry-after"], observed) if "retry-after" in safe_headers else None
    reset_at = source = None
    if failure_class == "rate" and provider_kind == "openai_compat" and provider_host == "api.openai.com":
        for key in ("x-ratelimit-reset-requests", "x-ratelimit-reset-tokens"):
            value = safe_headers.get(key)
            if value is None:
                continue
            candidate = _future(value, observed, duration=True) or _future(value, observed)
            if candidate is not None and (reset_at is None or candidate > reset_at):
                reset_at, source = candidate, key
    facts = {"status": status, "class": failure_class, "code": code, "reset": reset_at,
             "reset_source": source, "retry_after": retry_at, "provider_kind": provider_kind,
             "provider_host": provider_host}
    digest = hashlib.sha256(json.dumps(facts, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    safe_code = code if code in _CODES or code in {"authentication_error", "permission_error"} else None
    return FailureEvidence(status, failure_class, safe_code, reset_at, source, retry_at, digest)
