"""Operator preview and receipted cancellation of explicitly owned work."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.extensions.lifecycle import Lifecycle
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.lifecycle import LifecycleRefused

if TYPE_CHECKING:
    from daedalus.app import Application


class CancelBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_entity_revision: int = Field(ge=1, strict=True)
    expected_source_revision: int = Field(ge=1, strict=True)
    client_operation_id: str = Field(min_length=1, max_length=160)
    reason: str = Field(min_length=1, max_length=1000)
    preview_fingerprint: str = Field(pattern="^[a-f0-9]{64}$")


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    def service() -> Lifecycle:
        available = getattr(app, "extensions", {}).get("lifecycle")
        if not isinstance(available, Lifecycle):
            raise HTTPException(503, "lifecycle subsystem is unavailable")
        return available

    @api.get("/api/lifecycle/{parent_kind}/{parent_id}")
    async def preview(parent_kind: str, parent_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await service().preview(parent_kind, parent_id)
        except KeyError as exc:
            raise HTTPException(404, "no such parent") from exc
        except LifecycleRefused as exc:
            raise HTTPException(400, str(exc)) from exc

    @api.post("/api/lifecycle/{parent_kind}/{parent_id}/cancel")
    async def cancel(
        parent_kind: str, parent_id: str, body: CancelBody, who: dict[str, Any] = Depends(auth),
    ) -> dict[str, Any]:
        try:
            return await service().cancel_command(
                Principal.operator(who), parent_kind, parent_id, body.reason,
                expected_entity_revision=body.expected_entity_revision,
                expected_source_revision=body.expected_source_revision,
                client_operation_id=body.client_operation_id,
                preview_fingerprint=body.preview_fingerprint,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such parent") from exc
        except (LifecycleRefused, ControlConflict) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
