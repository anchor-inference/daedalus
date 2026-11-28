"""The model roles a live link can set and narrow: what the share dialog offers the operator.

A role is one place a session's work reaches a model: the conversation itself, its subagents, the reading
of pictures, translation and so on. For each, the dialog shows the value the session would use now and the
presets, accounts and efforts the operator may put in its place or leave open to the visitor.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from daedalus.config import REASONING_EFFORTS, keyproxy_upstream
from daedalus.host import subagent_models
from daedalus.host.share_spend import billing_of
from daedalus.providers.accounts import ACCOUNT_UPSTREAMS

if TYPE_CHECKING:
    from daedalus.app import Application

ROLES = (
    ("main", "Conversation", True, "visitor"),
    ("subagents", "Subagents", False, "agent"),
    ("quick", "Quick agent", False, "agent"),
    ("vision", "Picture reading", False, "none"),
)
"""``(id, label, pickable, chooser)``. Only the conversation has a control on a visitor's screen (the model
picker). The helpers' models are chosen by the agent, from a set, when it starts one: the installation lets
the operator name several, and a link narrows that set instead of the visitor. Vision has one value."""


async def describe(app: Application, session_id: str) -> list[dict[str, Any]]:
    """Every role of the session, in the order the dialog lists them.

    Each is ``{"id", "label", "pickable", "chooser", "current": {"preset", "effort", "account", "allowed"},
    "presets": [{"id", "label", "provider", "provider_name", "model", "billing", "efforts": [...],
    "accounts": [{"id", "name"}]}]}``. ``pickable`` says the visitor's own screen has a control for the role,
    so leaving it open means something; ``chooser`` says who picks within the role's set: ``visitor``,
    ``agent`` (a helper's model, named when the agent starts it) or ``none``. ``current.allowed`` is the
    set the session uses now, in the policy's shape, for a role whose agent chooses (the session's list of
    helper models, each with any effort); the value is ``preset``/``effort``/``account`` as before.
    ``billing`` is ``subscription`` or ``metered``, which decides whether the money limit or the weekly
    share applies to it.

    Only the roles a link's policy is enforced for are listed: a role the dialog offered and the runtime
    did not hold would be a limit the operator believes in and a visitor walks past."""
    manager = app.manager if hasattr(app, "manager") else app
    if manager is None:
        return []
    state = await manager.get_state(session_id)
    if state is None:
        raise KeyError(session_id)
    config = manager.config
    overrides = manager.task_overrides(state, await manager.live.load(session_id))
    available = set(manager.providers.available())
    ready = [pid for pid, preset in config.presets.items() if preset.provider in available and preset.model]

    def entry(pid: str) -> dict[str, Any]:
        preset = config.presets[pid]
        provider = config.providers.get(preset.provider)
        upstream = keyproxy_upstream(provider.base_url) if provider is not None else ""
        accounts = []
        if upstream in ACCOUNT_UPSTREAMS and manager.subscription_accounts.several(upstream):
            accounts = [{"id": a.id, "name": a.label or a.id} for a in manager.subscription_accounts.accounts(upstream)]
        return {
            "id": pid, "label": preset.display(pid), "provider": preset.provider,
            "provider_name": (provider.name if provider is not None and provider.name else preset.provider),
            "model": preset.model, "billing": billing_of(config, preset.provider),
            "efforts": ["none", *REASONING_EFFORTS] if preset.thinking else [], "accounts": accounts,
        }

    def account_for(pid: str) -> str:
        preset = config.presets.get(pid)
        provider = config.providers.get(preset.provider) if preset is not None else None
        upstream = keyproxy_upstream(provider.base_url) if provider is not None else ""
        if upstream not in ACCOUNT_UPSTREAMS or not manager.subscription_accounts.several(upstream):
            return ""
        return manager.subscription_accounts.chosen(session_id, upstream) or manager.account_default(overrides, upstream)

    main = manager.session_preset_id(overrides) or ""
    main_effort = "none" if overrides.get("thinking_enabled") is False else str(overrides.get("reasoning_effort") or "")
    if not main_effort and main in config.presets:
        main_effort = config.presets[main].reasoning_effort if config.presets[main].thinking else "none"
    mode, chosen, _ = subagent_models.choice(state.metadata, config.subagents.models)
    helpers = subagent_models.allowance(mode, chosen, available=ready, session_preset=main or None).presets
    quick = manager.quick_models(state.metadata)
    vision = manager.vision_preset_for(session_id)
    currents = {
        "main": (main, main_effort, ready),
        # A subagent runs on its leader's model unless the leader names one, so that is the current value.
        "subagents": (main if main in helpers or not helpers else helpers[0], config.subagents.models.effort, list(helpers) or ready),
        "quick": (quick[0] if quick else "", config.subagents.quick_effort, list(quick)),
        "vision": (vision[0] if vision else "", manager.vision_effort_for(session_id), [pid for pid in ready if config.presets[pid].images]),
    }
    # The installation's own lists name presets and no efforts: a helper may be started on any effort of them.
    sets = {"subagents": list(helpers), "quick": list(quick)}
    roles = []
    for role, label, pickable, chooser in ROLES:
        preset, effort, offered = currents[role]
        if not offered:
            # A role nothing can serve (no quick models chosen, no model that sees) has nothing to set.
            continue
        presets = [entry(pid) for pid in offered if pid in config.presets]
        # A model that takes no effort setting has none to name: its "none" is the absence of a choice,
        # and the dialog sending it back as a value was refused as an effort the preset does not have.
        if effort not in next((found["efforts"] for found in presets if found["id"] == preset), []):
            effort = ""
        listed = [pid for pid in sets.get(role, []) if pid in config.presets]
        account = account_for(preset) if preset else ""
        roles.append({
            "id": role, "label": label, "pickable": pickable, "chooser": chooser,
            # The value's preset keeps the value's account in the set: a helper on it is held to that account,
            # and "any" there would read as a choice the agent does not have.
            "current": {"preset": preset, "effort": effort, "account": account,
                        "allowed": [{"preset": pid, "accounts": [account] if pid == preset and account else [], "efforts": []} for pid in listed]},
            "presets": presets,
        })
    return roles


__all__ = ["ROLES", "describe"]
