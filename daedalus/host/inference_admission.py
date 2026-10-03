"""Pin every applicable dollar balance before a hosted model can begin charging it."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from protocore.contracts.llm import LLMProviderError, LLMRequest

from daedalus.providers.openai_compat import ProviderEndpoint, ProviderVerdict
from daedalus.providers.pricing import ModelPricing, complete_usage
from daedalus.stores.control import ControlDenied, canonical, one
from daedalus.stores.inference_budget import BudgetRefused, Constraint, InferenceBudget, microusd

if TYPE_CHECKING:
    from daedalus.host.session_runner import SessionManager


def maximum_rate(*values: float | None) -> Decimal:
    rates = [Decimal(str(value)) for value in values if value is not None]
    if not rates or any(not rate.is_finite() or rate < 0 for rate in rates):
        raise BudgetRefused("the rate card has an unknown or invalid price")
    return max(rates)


def model_quote(endpoint: ProviderEndpoint, model: str, output: int) -> tuple[dict[str, Any], int]:
    """Share the provider ceiling and worst published rate between launch and each real send."""
    if type(output) is not int or not 1 <= output <= 2**63 - 1:
        raise BudgetRefused('a finite output token cap is required before inference')
    free = endpoint.kind == 'llamacpp'
    price = endpoint.pricing.get(model)
    if free:
        input_bound = 0
        input_rate = output_rate = Decimal(0)
    else:
        if price is None or price.input is None or price.output is None or price.input_limit is None or not price.limit_source:
            raise BudgetRefused('this model needs a priced, provider-enforced input ceiling before a capped call')
        input_bound = price.input_limit
        if type(input_bound) is not int or not 1 <= input_bound <= 2**63 - 1:
            raise BudgetRefused("the provider's billable input ceiling is invalid")
        input_rate = maximum_rate(price.input, price.input_off_peak, price.cache_hit, price.cache_hit_off_peak)
        output_rate = maximum_rate(price.output, price.output_off_peak)
    quoted = microusd((input_rate * input_bound + output_rate * output) / Decimal(1_000_000))
    quote = {'provider_id': endpoint.id, 'provider_kind': endpoint.kind, 'model': model, 'input_bound': input_bound,
             'output_bound': output, 'input_rate': str(input_rate), 'output_rate': str(output_rate),
             'bound_source': price.limit_source if price is not None and not free else 'operator-local-model',
             'rate_card': asdict(price) if price is not None and not free else None}
    return quote, quoted


class HostInferenceAdmission:
    def __init__(self, manager: SessionManager) -> None:
        self.manager = manager
        self.db = manager.db
        self.store = InferenceBudget(self.db)

    async def quote_for_member(self, staff_id: str, *, slot_id: str, slot: int,
                               allowance_microusd: int) -> Any:
        async with self.db.transaction() as conn:
            return await self.quote_for_member_in(conn, staff_id, slot_id=slot_id, slot=slot,
                                                  allowance_microusd=allowance_microusd)

    async def quote_for_member_in(self, conn: Any, staff_id: str, *, slot_id: str, slot: int,
                                  allowance_microusd: int) -> Any:
        """Resolve the selected staff preset once; a comparison cannot spend on a fallback rung."""
        from daedalus.stores.comparison_funding import PairAllocation

        member = await one(conn, 'SELECT harness,model,archived_at FROM staff WHERE id = ?', (staff_id,))
        if member is None or member['harness'] != 'daedalus' or member['archived_at']:
            raise BudgetRefused('the comparison needs an active native worker')
        if member['model'] and member['model'] not in self.manager.config.presets:
            raise BudgetRefused('the worker model preset no longer exists')
        rungs, preset = self.manager.resolve_model({'preset': member['model']} if member['model'] else {})
        if not rungs:
            raise BudgetRefused('the selected comparison model is unavailable')
        adapter, model = rungs[0]
        quote, _ = model_quote(adapter.endpoint, model, int(preset.max_output_tokens))
        return PairAllocation(slot_id, slot, staff_id, adapter.endpoint.id, model, allowance_microusd,
                              hashlib.sha256(canonical(quote).encode()).hexdigest(), quote)

    def prelaunch_constraints(self, provider_ids: tuple[str, ...]) -> tuple[Constraint, ...]:
        """Current installation and provider caps before the alternative sessions exist."""
        manager = self.manager
        limits = manager.config.limits
        result = []
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
        if manager.settings.usd_per_day > 0:
            result.append(Constraint(f"daily:{today}", microusd(manager.settings.usd_per_day), since=today))
        since = limits.total_since or None
        if limits.usd_total > 0:
            result.append(Constraint(f"total:{since or 'all'}", microusd(limits.usd_total), since=since))
        for provider_id in dict.fromkeys(provider_ids):
            provider_cap = limits.usd_total_per_provider.get(provider_id, 0)
            if provider_cap > 0:
                result.append(Constraint(f"provider:{provider_id}:{since or 'all'}", microusd(provider_cap),
                                         provider_id=provider_id, since=since))
        return tuple(result)

    async def _constraints(self, conn: Any, endpoint: ProviderEndpoint, request: LLMRequest) -> tuple[Constraint, ...]:
        manager = self.manager
        limits = manager.config.limits
        result = list(self.prelaunch_constraints((endpoint.id,)))
        observer = request.observability
        if observer is None or not observer.session_id:
            return tuple(result)
        identity = observer.session_id
        seen = set()
        while identity:
            if identity in seen or len(seen) >= 256:
                raise BudgetRefused("the session's spending ownership is cyclic or too deep")
            seen.add(identity)
            row = await one(conn, "SELECT metadata FROM sessions WHERE id = ?", (identity,))
            if row is None:
                raise BudgetRefused("the charged session no longer exists")
            metadata = json.loads(row["metadata"])
            cap = metadata.get("usd_cap")
            if cap is not None:
                cursor = await conn.execute(
                    "WITH RECURSIVE owned(id) AS (SELECT ? UNION SELECT s.id FROM sessions s"
                    " JOIN owned o ON json_extract(s.metadata,'$.subagent_of') = o.id) SELECT id FROM owned",
                    (identity,),
                )
                descendants = tuple(row["id"] for row in await cursor.fetchall())
                await cursor.close()
                result.append(Constraint(f"session:{identity}", microusd(cap), session_ids=descendants))
            identity = str(metadata.get("subagent_of") or "")
        if observer.run_id:
            run = await one(conn, "SELECT session_id FROM runs WHERE id = ?", (observer.run_id,))
            if run is None or run["session_id"] != observer.session_id:
                raise BudgetRefused("the charged run belongs to another session")
            state = manager.live_state(observer.session_id)
            mode = manager.mode_for(state) if state is not None else None
            mode_cap = mode.usd_per_run if mode is not None else None
            cap = mode_cap if mode_cap is not None else limits.usd_per_run
            if cap > 0 or mode_cap is not None:
                result.append(Constraint(f"run:{observer.run_id}", microusd(cap), run_id=observer.run_id))
        return tuple(result)

    async def start(self, endpoint: ProviderEndpoint, request: LLMRequest, body: dict[str, Any]) -> str | None:
        try:
            async with self.db.transaction() as conn:
                observer = request.observability
                attempt = None
                if observer is not None and observer.session_id:
                    executions = getattr(self.manager, "execution_store", None)
                    if executions is not None:
                        attempt = await executions.check_inference(conn, observer.session_id, observer.run_id)
                    else:
                        worker = await one(conn, "WITH RECURSIVE owned(id) AS (SELECT ? UNION"
                                           " SELECT json_extract(s.metadata,'$.subagent_of') FROM sessions s"
                                           " JOIN owned o ON s.id = o.id"
                                           " WHERE json_extract(s.metadata,'$.subagent_of') IS NOT NULL)"
                                           " SELECT 1 FROM owned o JOIN sessions s ON s.id = o.id"
                                           " WHERE json_extract(s.metadata,'$.staff_session_id') IS NOT NULL"
                                           " OR EXISTS (SELECT 1 FROM staff_sessions w WHERE w.session_id = s.id) LIMIT 1",
                                           (observer.session_id,))
                        if worker is not None:
                            raise ControlDenied("worker inference requires the current host execution owner")
                free = endpoint.kind == "llamacpp"
                constraints = () if free else await self._constraints(conn, endpoint, request)
                price = endpoint.pricing.get(request.model)
                if not free and (price is None or price.input is None or price.output is None
                                 or price.input_limit is None or not price.limit_source):
                    # A prepaid comparison cannot silently send an unpriced fallback even when
                    # the operator has disabled broader daily or installation spending limits.
                    comparison = await one(conn, 'SELECT 1 FROM comparison_funding_slots WHERE attempt_id = ?',
                                           (attempt.id,)) if attempt is not None else None
                    if not constraints and comparison is None:
                        return None
                    raise BudgetRefused("this model needs a priced, provider-enforced input ceiling before a capped call")
                quote, quoted = model_quote(endpoint, request.model, body.get('max_tokens'))
                quote['quoted_at'] = datetime.now(UTC).isoformat()
                observer = request.observability
                reservation = uuid.uuid4().hex
                await self.store.reserve_in(
                    conn, reservation_id=reservation, provider_id=endpoint.id, model=request.model,
                    session_id=observer.session_id if observer else None, run_id=observer.run_id if observer else None,
                    request_digest=hashlib.sha256(canonical(body).encode()).hexdigest(), quoted_microusd=quoted,
                    rate_version=hashlib.sha256(canonical(quote).encode()).hexdigest(), quote=quote,
                    constraints=constraints,
                    execution_attempt_id=attempt.id if attempt is not None else None,
                )
                await self.store.start_in(conn, reservation)
                return reservation
        except BudgetRefused as exc:
            error = LLMProviderError(f"inference budget: {exc}")
            error.classified = ProviderVerdict("budget", False)
            raise error from exc
        except ControlDenied as exc:
            error = LLMProviderError(f"inference authority: {exc}")
            error.classified = ProviderVerdict("authority", False)
            raise error from exc

    async def cost(self, reservation_id: str, raw: dict[str, Any], normalized: dict[str, Any]) -> float | None:
        row = await self.db.fetchone("SELECT quote_json FROM inference_reservations WHERE id = ?", (reservation_id,))
        if row is None:
            raise BudgetRefused("the inference has no admitted rate card")
        quote = json.loads(row["quote_json"])
        if quote["provider_kind"] == "llamacpp":
            return 0.0
        if raw.get("cost") is not None:
            return microusd(raw["cost"]) / 1_000_000
        if not complete_usage(raw):
            return None
        rate = ModelPricing.from_entry(quote["rate_card"])
        return rate.cost(normalized, now=datetime.fromisoformat(quote["quoted_at"]))

    async def interrupted(self, reservation_id: str, reason: str) -> None:
        async with self.db.transaction() as conn:
            await self.store.unknown_in(conn, reservation_id, reason)
