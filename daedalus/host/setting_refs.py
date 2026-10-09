"""Which setting a problem is about, so the operator is taken to it instead of told where to look.

A refusal that names a setting ("the summary model is over its limit", "no vision model is set") used
to end in a sentence: the operator then had to find the page, the card and the row on their own. A
:class:`SettingRef` is the address of that row — the Settings section it lives on and the
configuration path the row is anchored by (``data-setting`` in the app) — and it travels beside the
message: as the ``setting`` field of an HTTP error, of a ``setting_notice`` session event and of a
notification, whose link opens the row and makes it blink.

The references are named here, once, so the backend and the app agree on them; the app's test of its
anchors reads the same keys.
"""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlsplit

SETTINGS_PATH = "/app/settings"


@dataclass(frozen=True)
class SettingRef:
    """A setting row: ``page`` is the Settings section, ``key`` the configuration path it edits."""

    page: str
    key: str

    def as_dict(self) -> dict[str, str]:
        return {"page": self.page, "key": self.key}

    def link(self) -> str:
        """The app address that opens the section, scrolls to the row and highlights it."""
        return f"{SETTINGS_PATH}/{quote(self.page)}?setting={quote(self.key, safe='')}"


COMPACTION_MODEL = SettingRef("limits", "compaction.preset")
VISION_MODEL = SettingRef("models", "vision.preset")
VISION_OUTPUT = SettingRef("models", "vision.max_output_tokens")
AUXILIARY_FALLBACK = SettingRef("models", "model.fallback_to_session")
SPEECH_RECOGNITION = SettingRef("voice", "asr.transcriber")
LOCAL_SPEECH_MODEL = SettingRef("voice", "stt.local_model")
SPEND_CAP = SettingRef("limits", "limits.usd_total")
RUN_SPEND_CAP = SettingRef("limits", "limits.usd_per_run")
WEB_SEARCH = SettingRef("tools", "tools.web.search.backend")


def provider_spend_cap(provider_id: str) -> SettingRef:
    """One provider's total spend cap, on Settings → Limits."""
    return SettingRef("limits", f"limits.usd_total_per_provider.{provider_id}")


def preset_images(preset_id: str) -> SettingRef:
    """One model's Images switch, in its row on Settings → Models."""
    return SettingRef("models", f"presets.{preset_id}.images")


def provider_key(provider_id: str) -> SettingRef:
    """The key of one provider endpoint, on the providers tab of Settings → Models."""
    return SettingRef("models", f"providers.{provider_id}.api_key")


class SettingProblem(RuntimeError):
    """A refusal that one setting answers. A ``RuntimeError``, so every caller that already turns
    "this cannot be done now" into a message (the API's 409, the chat's reply) needs nothing new and
    the ones that know about settings can offer the way to the row."""

    def __init__(self, message: str, setting: SettingRef) -> None:
        super().__init__(message)
        self.setting = setting


def setting_of(exc: BaseException | None) -> SettingRef | None:
    """The setting an exception, or anything it was raised from, is about; ``None`` when none is named.

    The chain is followed because a refusal is often re-raised as something more general on its way
    up (a vision failure as a browser "environment unavailable"), and the reference must survive it."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        found = getattr(exc, "setting", None)
        if isinstance(found, SettingRef):
            return found
        exc = exc.__cause__ or exc.__context__
    return None


def setting_from_link(link: str | None) -> SettingRef | None:
    """The reference a link written by :meth:`SettingRef.link` carries, so a stored notification,
    which keeps only its link, still offers the way to its setting when it is read back."""
    if not link or not link.startswith(SETTINGS_PATH + "/"):
        return None
    parts = urlsplit(link)
    page = parts.path[len(SETTINGS_PATH) + 1:].strip("/")
    key = (parse_qs(parts.query).get("setting") or [""])[0]
    return SettingRef(page, key) if page and key else None


__all__ = [
    "AUXILIARY_FALLBACK",
    "COMPACTION_MODEL",
    "LOCAL_SPEECH_MODEL",
    "RUN_SPEND_CAP",
    "SPEECH_RECOGNITION",
    "SPEND_CAP",
    "VISION_MODEL",
    "VISION_OUTPUT",
    "WEB_SEARCH",
    "SettingProblem",
    "SettingRef",
    "preset_images",
    "provider_key",
    "provider_spend_cap",
    "setting_from_link",
    "setting_of",
]
