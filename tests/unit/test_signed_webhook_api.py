"""Signed ingress acknowledges only durable facts and rejects ambiguous delivery identities."""

from __future__ import annotations

import hashlib
import hmac
import json
from types import SimpleNamespace

import httpx

from daedalus.config import RuntimeConfig, Settings, WebhookConfig
from daedalus.extensions.api import build_app
from daedalus.extensions.inbound import Inbound
from daedalus.host.session_runner import SessionManager
from daedalus.stores.database import Database


async def test_signed_ingress_is_atomic_replayable_and_bound_to_the_received_body(
    settings: Settings, config: RuntimeConfig, db: Database,
) -> None:
    manager = SessionManager(settings, config, db=db)
    await manager.start()
    app = SimpleNamespace(settings=settings, config=config, db=db, manager=manager,
                          front=None, extensions={}, guard=None, notifications=None)
    app.extensions["inbound"] = Inbound(app)
    config.webhooks["github"] = WebhookConfig(secret="test-webhook-secret", scheme="github", deliver="events")
    api = build_app(app, "tok")
    body = {"repository": {"id": 7}, "check_run": {"id": 50, "name": "unit",
            "head_sha": "a" * 40, "status": "completed", "conclusion": "success"}}
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=api), base_url="http://test") as client:
            async def post(payload, delivery="delivery50", valid=True, event="check_run"):
                raw = json.dumps(payload).encode()
                signature = hmac.new(b"test-webhook-secret", raw, hashlib.sha256).hexdigest() if valid else "0" * 64
                return await client.post("/webhooks/github", content=raw,
                                         headers={"X-GitHub-Event": event, "X-GitHub-Delivery": delivery,
                                                  "X-Hub-Signature-256": f"sha256={signature}"})

            assert (await post(body, valid=False)).status_code == 401
            assert await db.fetchall("SELECT * FROM ci_observations") == []
            await db.execute("CREATE TRIGGER reject_event BEFORE INSERT ON app_events WHEN NEW.type = 'webhook.received'"
                             " BEGIN SELECT RAISE(ABORT,'event storage unavailable'); END")
            assert (await post(body)).status_code == 503
            assert await db.fetchall("SELECT * FROM webhook_deliveries") == []
            assert await db.fetchall("SELECT * FROM ci_observations") == []
            await db.execute("DROP TRIGGER reject_event")
            first = await post(body)
            assert first.status_code == 200 and first.json()["status"] == "accepted", first.text
            assert (await post(body)).json()["status"] == "duplicate"
            changed = {**body, "check_run": {**body["check_run"], "conclusion": "failure"}}
            assert (await post(changed)).status_code == 409
            assert (await db.fetchone("SELECT count(*) FROM ci_observations"))[0] == 1
            assert (await db.fetchone("SELECT count(*) FROM app_events WHERE type = 'webhook.received'"))[0] == 1
            assert (await post(["deploy"], "generic", event="deployment_status")).status_code == 200
            assert (await post(["different"], "generic", event="deployment_status")).status_code == 409
    finally:
        await manager.close()
