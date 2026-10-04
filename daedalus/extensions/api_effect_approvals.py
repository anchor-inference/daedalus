"""Operator decisions for exact, committed effects."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Header, HTTPException

from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.effect_approvals import EffectApprovals

if TYPE_CHECKING:
    from daedalus.app import Application


def install_routes(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.post("/api/effects/{effect_id}/approve")
    async def approve(effect_id: str, body: dict[str, Any], if_match: str | None = Header(default=None),
                      who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            if if_match is None or not if_match.isdecimal() or int(if_match) < 1:
                raise ValueError("If-Match must name the current collection revision")
            if body.get("decision") != "allow":
                raise ValueError("decision must be allow")
            for field in ("expected_artifact_revision", "expires_at", "client_operation_id"):
                if field not in body:
                    raise ValueError(f"missing field: {field}")
            result = await EffectApprovals(app.db).approve(
                effect_id, Principal.operator(who), expected_artifact_revision=body["expected_artifact_revision"],
                expires_at=body["expires_at"], client_operation_id=body["client_operation_id"],
                expected_collection_revision=int(if_match),
            )
            dispatcher = app.extensions.get("effects")
            if dispatcher is not None:
                dispatcher.notify()
            return result
        except KeyError as exc:
            raise HTTPException(404, "no such effect") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/effect-approvals/{approval_id}/revoke")
    async def revoke(approval_id: str, body: dict[str, Any], if_match: str | None = Header(default=None),
                     who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            if if_match is None or not if_match.isdecimal() or int(if_match) < 1:
                raise ValueError("If-Match must name the current collection revision")
            if "client_operation_id" not in body:
                raise ValueError("missing field: client_operation_id")
            return await EffectApprovals(app.db).revoke(
                approval_id, Principal.operator(who), client_operation_id=body["client_operation_id"],
                expected_collection_revision=int(if_match),
            )
        except KeyError as exc:
            raise HTTPException(404, "no such approval") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except ControlConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        except (TypeError, ValueError) as exc:
            raise HTTPException(422, str(exc)) from exc
