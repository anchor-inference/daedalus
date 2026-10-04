"""The operator's project goal money limit and its current held balance."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from daedalus.host.inference_admission import model_quote
from daedalus.stores.control import ControlConflict, ControlDenied, ControlStore, Entity, Principal, Scope
from daedalus.stores.goal_budget import set_budget_in, usd, view_in
from daedalus.stores.inference_budget import BudgetRefused

if TYPE_CHECKING:
    from daedalus.app import Application


class BudgetBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    limit_usd: str
    coordination_limit_usd: str
    expected_goal_revision: int = Field(ge=1)
    expected_entity_revision: int = Field(ge=1)
    client_operation_id: str = Field(min_length=1, max_length=160)


def register(api: FastAPI, app: Application, auth: Callable[..., Any]) -> None:
    @api.get("/api/projects/{project_id}/budget")
    async def read_budget(project_id: str, _: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        async with app.db.transaction() as conn:
            cursor = await conn.execute("SELECT entity_revision,goal_revision,settings FROM projects WHERE id = ?",
                                        (project_id,))
            project = await cursor.fetchone()
            await cursor.close()
            if project is None:
                raise HTTPException(404, "no such project")
            view = await view_in(conn, project_id)
            coordinator_quote = None
            manager = getattr(app, "manager", None)
            if manager is not None:
                choice = json.loads(project["settings"]).get("orchestrator", {}).get("model", "")
                preset_id = manager.config.orchestrator_preset(choice)
                if preset_id:
                    try:
                        rungs, preset = manager.resolve_model({"preset": preset_id})
                        if rungs:
                            adapter, model = rungs[0]
                            quote, amount = model_quote(adapter.endpoint, model, preset.max_output_tokens)
                            coordinator_quote = {"model": model, "reserve_usd": usd(amount),
                                                 "input_bound": quote["input_bound"],
                                                 "output_bound": quote["output_bound"]}
                    except (BudgetRefused, KeyError, RuntimeError, ValueError):
                        # A balance remains readable while the selected model is being configured.
                        pass
            return {"configured": view is not None, "entity_revision": project["entity_revision"],
                    "goal_revision": project["goal_revision"], "coordinator_quote": coordinator_quote,
                    **(view or {"project_id": project_id})}

    @api.put("/api/projects/{project_id}/budget")
    async def set_budget(project_id: str, body: BudgetBody,
                         who: dict[str, Any] = Depends(auth)) -> dict[str, Any]:
        principal = Principal.operator(who)

        async def effect(conn: Any, mutation: Any) -> dict[str, Any]:
            return await set_budget_in(conn, project_id=project_id, budget_id=mutation.object_id,
                                       expected_goal_revision=body.expected_goal_revision,
                                       limit_usd=body.limit_usd,
                                       coordination_limit_usd=body.coordination_limit_usd)

        try:
            return await ControlStore(app.db).mutate(
                principal, Scope("project", project_id), "budget.set", body.client_operation_id,
                body.expected_entity_revision, Entity("project", project_id), body.model_dump(), effect,
            )
        except KeyError as exc:
            raise HTTPException(404, "no such project") from exc
        except ControlDenied as exc:
            raise HTTPException(403, str(exc)) from exc
        except (ControlConflict, BudgetRefused) as exc:
            raise HTTPException(409, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
