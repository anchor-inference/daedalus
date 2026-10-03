"""Per-model prices (USD per one million tokens) and the peak/off-peak schedule.

The built-in table is the published DeepSeek list; ``[providers.<id>.pricing.<model>]``
in the config overrides or extends it. Off-peak is everything outside the peak
windows. DeepSeek's schedule (api-docs.deepseek.com, "Models & Pricing", read 2026-09-10):
"Off-peak rates are half of the peak rates. Peak hours are 01:00-04:00 and 06:00-10:00 UTC,
Monday through Friday (all other hours are off-peak)."

The current model is ``deepseek-flash`` (DeepSeek-V4.1-Flash). The legacy names
``deepseek-v4-flash`` and ``deepseek-v4-flash-vision-exp`` are still accepted, served by
V4.1 Flash and billed at the Flash price. ``deepseek-v4-pro`` keeps its own price until
2026-09-14 12:00 Beijing time, after which it is routed to V4.1 Flash and billed as Flash.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any


def complete_usage(raw: dict[str, Any]) -> bool:
    counts = next((keys for keys in (("prompt_tokens", "completion_tokens"), ("input_tokens", "output_tokens"))
                   if all(key in raw for key in keys)), None)
    return counts is not None and all(type(raw[key]) is int and raw[key] >= 0 for key in counts)


DEEPSEEK_PEAK_UTC = ("01:00-04:00", "06:00-10:00")


@dataclass(slots=True)
class ModelPricing:
    input: float | None = 0.0
    output: float | None = 0.0
    cache_hit: float | None = 0.0
    input_off_peak: float | None = None
    output_off_peak: float | None = None
    cache_hit_off_peak: float | None = None
    peak_utc: tuple[str, ...] = ()
    """Peak windows as ``HH:MM-HH:MM`` in UTC; empty means the base prices always apply."""
    peak_weekdays_only: bool = True
    input_limit: int | None = None
    limit_source: str = ""
    """The provider-enforced billable input ceiling; a prompt size estimate cannot reserve money."""

    def off_peak_at(self, now: datetime) -> bool:
        if not self.peak_utc:
            return False
        if self.peak_weekdays_only and now.weekday() >= 5:
            return True
        minutes = now.hour * 60 + now.minute
        for window in self.peak_utc:
            start_s, _, end_s = window.partition("-")
            start = int(start_s[:2]) * 60 + int(start_s[3:5])
            end = int(end_s[:2]) * 60 + int(end_s[3:5])
            inside = start <= minutes < end if start <= end else minutes >= start or minutes < end
            if inside:
                return False
        return True

    def cost(self, usage: dict[str, Any], *, now: datetime | None = None) -> float | None:
        cache_hit = int(usage.get("cache_read_tokens") or 0)
        prompt = int(usage.get("input_tokens") or 0)
        fresh = max(prompt - cache_hit, 0)
        output = int(usage.get("output_tokens") or 0)
        off = self.off_peak_at(now or datetime.now(UTC))
        p_in = self.input_off_peak if off and self.input_off_peak is not None else self.input
        p_out = self.output_off_peak if off and self.output_off_peak is not None else self.output
        p_hit = self.cache_hit_off_peak if off and self.cache_hit_off_peak is not None else self.cache_hit
        if p_in is None or p_out is None:
            return None
        # An omitted cache rate cannot make cached input free; reserve the fresh-input rate.
        p_hit = p_in if p_hit is None else p_hit
        return (fresh * p_in + cache_hit * p_hit + output * p_out) / 1_000_000

    @classmethod
    def from_entry(cls, entry: dict[str, Any]) -> ModelPricing:
        peak = entry.get("peak_utc") or ()
        if isinstance(peak, str):
            peak = (peak,) if peak else ()
        weekdays_only = bool(entry.get("peak_weekdays_only", True))
        legacy = str(entry.get("off_peak_utc") or "")
        if not peak and "-" in legacy:
            # Older entries named the off-peak window; peak is its complement, every day.
            start, _, end = legacy.partition("-")
            peak = (f"{end}-{start}",)
            weekdays_only = False
        return cls(
            input_limit=int(entry["input_limit"]) if entry.get("input_limit") is not None else None,
            limit_source=str(entry.get("limit_source") or ""),
            input=_opt(entry.get("input")),
            output=_opt(entry.get("output")),
            cache_hit=_opt(entry.get("cache_hit")),
            input_off_peak=_opt(entry.get("input_off_peak")),
            output_off_peak=_opt(entry.get("output_off_peak")),
            cache_hit_off_peak=_opt(entry.get("cache_hit_off_peak")),
            peak_utc=tuple(str(w) for w in peak),
            peak_weekdays_only=weekdays_only,
        )


def _opt(value: Any) -> float | None:
    return None if value is None else float(value)


def _deepseek(input_: float, cache_hit: float, output: float) -> ModelPricing:
    return ModelPricing(
        input_limit=1_048_576,
        limit_source="https://api-docs.deepseek.com/quick_start/pricing/",
        input=input_,
        cache_hit=cache_hit,
        output=output,
        input_off_peak=input_ / 2,
        cache_hit_off_peak=cache_hit / 2,
        output_off_peak=output / 2,
        peak_utc=DEEPSEEK_PEAK_UTC,
    )


BUILTIN: dict[str, dict[str, ModelPricing]] = {
    "deepseek": {
        "deepseek-flash": _deepseek(0.30, 0.006, 1.20),
        "deepseek-v4-flash": _deepseek(0.30, 0.006, 1.20),
        "deepseek-v4-flash-vision-exp": _deepseek(0.30, 0.006, 1.20),
        "deepseek-v4-pro": _deepseek(1.32, 0.044, 3.96),
        "deepseek-chat": _deepseek(0.30, 0.006, 1.20),
        "deepseek-reasoner": _deepseek(0.30, 0.006, 1.20),
    },
}
"""Keyed by provider kind, then model name (prefix match at lookup time)."""


def pricing_table(kind: str, configured: dict[str, dict[str, Any]]) -> dict[str, ModelPricing]:
    """Built-in prices for ``kind`` overlaid with the operator's ``pricing`` entries."""
    table = dict(BUILTIN.get(kind, {}))
    for model, entry in configured.items():
        merged = asdict(table[model]) if model in table else {}
        for field in ("input", "output", "cache_hit"):
            # A changed base price cannot retain the previous rate card's cheaper schedule.
            # Unchanged prices and the independent provider input ceiling remain available.
            scheduled = f"{field}_off_peak"
            if field in entry and scheduled not in entry:
                merged[scheduled] = None
        table[model] = ModelPricing.from_entry({**merged, **entry})
    return table


__all__ = ["BUILTIN", "DEEPSEEK_PEAK_UTC", "ModelPricing", "pricing_table"]
