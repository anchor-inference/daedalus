"""The terminal client's devices and the live ``remote`` mark, and their Telegram side.

Installs :class:`daedalus.remote.devices.Devices` and :class:`daedalus.remote.control.RemoteControl`
before the API, which reads them for its door and routes (``api_remote``). In Telegram a terminal that
asks to pair is announced with Allow and Deny buttons, and ``/devices`` lists the paired ones with a
Revoke button each. The contract is ``docs/architecture/cli.md``.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from daedalus.host.events import EventFilter
from daedalus.remote.control import RemoteControl
from daedalus.remote.devices import Devices, Refused

if TYPE_CHECKING:
    from daedalus.app import Application

logger = logging.getLogger(__name__)

CALLBACK = "dv"
"""Telegram buttons: ``dv:<pairing id>:allow``, ``dv:<pairing id>:deny``, ``dv:<device id>:revoke``."""


def pairing_text(payload: dict[str, Any]) -> str:
    """The Telegram message for a terminal that asks to pair. The code is the point: the operator
    compares it with the one on their terminal before allowing, which is what tells their own request
    from someone else's."""
    where = " · ".join(part for part in (payload.get("hostname"), payload.get("os"), payload.get("address")) if part)
    return (
        f"💻 A terminal asks to pair: {payload.get('name') or 'terminal'}" + (f" ({where})" if where else "") + "\n\n"
        f"Code: {payload.get('user_code')}\n\n"
        "Allow it only if you just ran `daedalus-cli login` and your terminal shows this code."
    )


async def install(app: Application) -> list[asyncio.Task[None]]:
    manager = app.manager
    assert manager is not None
    bus = manager.bus

    async def publish(name: str, payload: dict[str, Any]) -> None:
        await bus.publish(name, payload)

    async def publish_for(name: str, payload: dict[str, Any], session_id: str | None) -> None:
        await bus.publish(name, payload, session_id=session_id)

    devices = Devices(app.db, publish=publish)
    control = RemoteControl(manager.control_marks, publish=publish_for, drop_presence=manager.presence.drop)
    devices.revoked_hooks.append(control.forget_device)
    app.extensions["remote_devices"] = devices
    app.extensions["remote_control"] = control
    tasks: list[asyncio.Task[None]] = [asyncio.create_task(control.watch(), name="remote-control")]

    front = app.front
    if front is not None:

        async def announce(event: Any) -> None:
            payload = dict(event.payload)
            if payload.get("state") != "pending":
                return
            outbox = front._general_outbox()
            if outbox is None:
                return
            pairing_id = str(payload.get("id") or "")
            await front.send_choice(outbox, pairing_text(payload), [[("✅ Allow", f"{CALLBACK}:{pairing_id}:allow"), ("✖ Deny", f"{CALLBACK}:{pairing_id}:deny")]])

        async def on_callback(query: Any, data: list[str]) -> None:
            if len(data) != 3 or data[2] not in ("allow", "deny", "revoke"):
                await query.answer("stale button")
                return
            _, ident, action = data
            if action == "revoke":
                said = "Revoked: its token no longer works." if await devices.revoke(ident) else "That device was already revoked."
            else:
                try:
                    decided = await devices.decide(await devices.pairing_by_id(ident), approve=action == "allow", via="telegram")
                except Refused as exc:
                    said = str(exc)
                else:
                    said = f"Allowed {decided.name}: the terminal goes on in a moment." if action == "allow" else f"Denied {decided.name}."
            await query.answer(said[:200])
            if query.message is not None:
                try:
                    await query.message.edit_text(f"{query.message.text}\n\n→ {said}", reply_markup=None)
                except Exception:  # noqa: BLE001 — the decision stands; an old message keeps its buttons
                    pass

        async def cmd_devices(message: Any, command: Any) -> None:
            paired = await devices.paired()
            if not paired:
                await message.answer("No terminal is paired. Run `daedalus-cli login <server address>` on one.")
                return
            counts = await devices.session_counts()
            outbox = front.outbox(message.chat.id, message.message_thread_id if message.is_topic_message else None)
            for device in paired:
                state = "connected" if control.connected(device.id) else f"last seen {device.last_seen_at[:16].replace('T', ' ')} UTC"
                text = f"💻 {device.name} — {device.hostname or '?'} · {device.os or '?'}\n{state} · {counts.get(device.id, 0)} conversation(s)"
                await front.send_choice(outbox, text, [[("Revoke", f"{CALLBACK}:{device.id}:revoke")]])

        front.callback_hooks[CALLBACK] = on_callback
        front.command_hooks["devices"] = cmd_devices
        tasks.append(bus.on(EventFilter(types=("device.pairing",)), announce, name="remote-pairing-telegram"))
    return tasks


__all__ = ["CALLBACK", "install", "pairing_text"]
