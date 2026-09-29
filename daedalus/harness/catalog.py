"""What the installation knows about its command-line agents, for the hiring form, the orchestrator's
``Harnesses`` tool and the Harnesses screen.

This is the reading side: the capability table joined with the last check of each environment.
Checking, installing and updating write the rows through the same store.
"""

from __future__ import annotations

from typing import Any

from daedalus.harness import ADAPTERS
from daedalus.harness.capabilities import CAPABILITIES, Capabilities, capabilities, version_supported, version_tested
from daedalus.stores.harness import CatalogRow, HarnessStore


def never_checked(env: str, harness: str) -> dict[str, Any]:
    """The row of a harness no check has recorded in ``env`` yet, with every key a checked row has,
    so a reader never has to ask which kind of row it holds."""
    return {
        "env": env, "harness": harness, "installed": False, "installed_version": "", "latest_version": "", "install_method": "", "binary_path": "",
        "logged_in": "unknown", "login_detail": "", "agents": [], "models": [], "modes": [], "efforts": [], "profiles": [], "self_check": {},
        "checked_at": None, "latest_checked_at": None, "error": "",
    }


def version_guard(view: dict[str, Any]) -> str:
    """How far the installed version is to be trusted: empty when it is one the adapter was tested
    against (or nothing is installed); ``verified`` when it is outside that range but the self-check
    has passed on this very version; ``unverified`` when it is outside and no passing self-check on it
    exists yet.

    A CLI that updated itself past what the adapter knows may have renamed a hook or redrawn a
    dialog. The hiring form and the orchestrator's ``Hire`` warn while it is ``unverified``: the
    member will be hired, but its first launch is the first time anyone sees whether it works.
    """
    version = str(view.get("installed_version") or "")
    if not view.get("installed") or not version or view.get("tested"):
        return ""
    check = view.get("self_check") or {}
    return "verified" if check.get("ok") and check.get("version") == version else "unverified"


def offered_models(listed: list[str], offered: list[str] | None) -> list[str]:
    """The chosen models in the CLI's own order, then any chosen one its last check did not list: a
    model the operator named is still named after a check that could not read the list."""
    if offered is None:
        return list(listed)
    return [m for m in listed if m in offered] + [m for m in offered if m not in listed]


class HarnessCatalog:
    def __init__(self, store: HarnessStore) -> None:
        self.store = store

    def capabilities(self, harness: str) -> Capabilities:
        return capabilities(harness)

    async def harnesses(self, env: str) -> list[dict[str, Any]]:
        """One entry per command-line harness, checked or not, in the table's order.

        A harness never checked in ``env`` is listed as not installed rather than left out: the
        hiring form shows every executor and says why one cannot be chosen.
        """
        rows = {row.harness: row for row in await self.store.catalog_rows(env)}
        return [self._entry(env, caps, rows.get(name), await self.store.offered_models(env, name)) for name, caps in CAPABILITIES.items()]

    def _entry(self, env: str, caps: Capabilities, row: CatalogRow | None, offered: list[str] | None = None) -> dict[str, Any]:
        """``models`` is what is offered — the operator's choice, or everything when there is none —
        and ``all_models`` everything the CLI listed. Every model stays usable either way: the choice
        only shortens what the hiring form and the orchestrator are shown first."""
        view: dict[str, Any] = row.view() if row is not None else never_checked(env, caps.harness)
        version = view["installed_version"]
        view.update(
            all_models=list(view["models"]),
            models=offered_models(view["models"], offered),
            models_chosen=offered is not None,
        )
        view.update(
            label=caps.label,
            status_channel=caps.status_channel,
            status_channel_label=caps.status_channel_label,
            steer=caps.steer,
            tested_versions=list(caps.tested_versions),
            # Derived here, not stored: the tested range moves with the adapter's code.
            tested=bool(version) and version_tested(caps, version),
            supported=bool(version) and version_supported(caps, version),
            adapter=caps.harness in ADAPTERS,
        )
        view["version_guard"] = version_guard(view)
        return view


__all__ = ["HarnessCatalog"]
