"""Operator commands for two prepaid alternatives under one immutable task contract."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException

from daedalus.extensions.comparison_commands import (
    choose_comparison,
    close_comparison,
    comparison_state,
    queue_comparison,
)
from daedalus.extensions.comparisons import ComparisonRefused
from daedalus.stores.control import ControlConflict, ControlDenied, Principal
from daedalus.stores.inference_budget import BudgetRefused

if TYPE_CHECKING:
    from daedalus.app import Application


def _priced_usd(value: Any) -> int:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("a decimal USD amount is required")
    try:
        amount = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("the USD amount must be decimal") from exc
    if not amount.is_finite() or not Decimal('0.000001') <= amount <= Decimal(2**63 - 1) / 1_000_000:
        raise ValueError("the USD amount must be positive and fit the ledger")
    micros = amount * 1_000_000
    if not amount.is_finite() or not 0 < micros <= 2**63 - 1 or micros != micros.to_integral_value():
        raise ValueError("the USD amount must be positive and precise to one microdollar")
    return int(micros)


def install_routes(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/board/{task_id}/comparisons/{group_id}")
    async def read_comparison(task_id: str, group_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            return await comparison_state(app, task_id=task_id, group_id=group_id)
        except KeyError as exc:
            raise HTTPException(404, "comparison not found") from exc

    @api.post("/api/board/{task_id}/comparisons/{group_id}/choose")
    async def select_comparison(task_id: str, group_id: str, body: dict[str, Any],
                                who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if set(body) != {"client_operation_id", "expected_entity_revision", "result_id", "verdict_id"}:
            raise HTTPException(422, "the selection needs exact result, verdict and command identity")
        if (not isinstance(body["client_operation_id"], str) or
                type(body["expected_entity_revision"]) is not int or
                not isinstance(body["result_id"], str) or not body["result_id"] or
                not isinstance(body["verdict_id"], str) or not body["verdict_id"]):
            raise HTTPException(422, "the selection fields are invalid")
        try:
            return await choose_comparison(app, task_id=task_id, group_id=group_id,
                                           principal=Principal.operator(who),
                                           client_operation_id=body["client_operation_id"],
                                           expected_entity_revision=body["expected_entity_revision"],
                                           result_id=body["result_id"], verdict_id=body["verdict_id"])
        except KeyError as exc:
            raise HTTPException(404, "task not found") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, ComparisonRefused) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/board/{task_id}/comparisons/{group_id}/close")
    async def close_pair(task_id: str, group_id: str, body: dict[str, Any],
                         who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        if (set(body) != {"client_operation_id", "expected_entity_revision"} or
                not isinstance(body["client_operation_id"], str) or
                type(body["expected_entity_revision"]) is not int):
            raise HTTPException(422, "the closure needs its command ID and task revision")
        try:
            return await close_comparison(app, task_id=task_id, group_id=group_id,
                                          principal=Principal.operator(who),
                                          client_operation_id=body["client_operation_id"],
                                          expected_entity_revision=body["expected_entity_revision"])
        except KeyError as exc:
            raise HTTPException(404, "task not found") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, ComparisonRefused, BudgetRefused) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/board/{task_id}/comparisons")
    async def launch_comparison(task_id: str, body: dict[str, Any],
                                who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        try:
            expected_fields = {"client_operation_id", "expected_entity_revision", "contract_revision",
                               "budget_cap_usd", "alternatives"}
            if set(body) != expected_fields:
                raise ValueError("the comparison command has missing or unknown fields")
            alternatives = body["alternatives"]
            if not isinstance(alternatives, list) or len(alternatives) != 2 or any(not isinstance(item, dict) for item in alternatives):
                raise ValueError("exactly two alternative workers are required")
            allowed = {"staff_id", "allowance_usd"}
            if any(set(item) != allowed for item in alternatives):
                raise ValueError("each alternative needs only staff_id and allowance_usd")
            priced = tuple({"staff_id": item["staff_id"], "allowance_microusd": _priced_usd(item["allowance_usd"])}
                           for item in alternatives)
            cap = _priced_usd(body["budget_cap_usd"])
            if type(body["expected_entity_revision"]) is not int or type(body["contract_revision"]) is not int:
                raise ValueError("expected and contract revisions must be integers")
            if not isinstance(body["client_operation_id"], str):
                raise ValueError("client_operation_id must be a string")
            return await queue_comparison(app, task_id=task_id, principal=Principal.operator(who),
                                          client_operation_id=body["client_operation_id"],
                                          expected_entity_revision=body["expected_entity_revision"],
                                          contract_revision=body["contract_revision"],
                                          budget_cap_microusd=cap, alternatives=(priced[0], priced[1]))
        except KeyError as exc:
            raise HTTPException(404, "task not found") from exc
        except (ValueError, BudgetRefused) as exc:
            raise HTTPException(422, str(exc)) from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, ComparisonRefused) as exc:
            raise HTTPException(409, str(exc)) from exc


__all__ = ["install_routes"]
