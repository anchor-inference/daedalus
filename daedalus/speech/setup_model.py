"""The speech model the setup wizard chose, fetched once the application is up.

The wizard runs before this program exists, so it can only write its choice down
(``DAEDALUS_STT_MODEL``, which :func:`daedalus.config._seed_from_setup` turns into the voice-note
transcriber and records as ``stt-model:<id>`` in ``seeded``; the environment is read there and only
there). The model itself is downloaded here, by the same :class:`~daedalus.speech.models.Downloads`
that Settings → Voice & speech drives, so its card there shows the bar moving and a failure in the
words it always uses.

It is fetched once. The marker goes into ``seeded`` only when the model is installed: a native first
start installs the speech engine and restarts right after, which cancels a download in flight, and the
next start has to carry on from the part file rather than believe it was done. After that a model the
operator deletes stays deleted, and one they replaced with another transcriber is never fetched at all.
"""

from __future__ import annotations

import logging
from typing import Any

from daedalus.speech import catalog

logger = logging.getLogger(__name__)

GIVEN_UP = frozenset({"queued", "failed", "cancelled", "deleted"})
"""Progress states after which waiting longer is pointless. ``queued`` is a refusal — another model is
downloading and nothing starts this one later. A failure is on the Settings card; the next start tries
again, since the marker was not written."""


def marker(model_id: str) -> str:
    return f"stt-fetched:{model_id}"


def wanted(config: Any) -> str:
    """The model to fetch now, or empty: chosen in the wizard, still the transcriber, never fetched."""
    model_id = config.asr.transcriber
    if model_id not in catalog.BY_ID or f"stt-model:{model_id}" not in config.seeded:
        return ""
    return "" if marker(model_id) in config.seeded else model_id


async def fetch_setup_model(app: Any) -> None:
    """Download the wizard's model if it is still wanted, and remember that it arrived."""
    model_id = wanted(app.config)
    if not model_id:
        return
    downloads = app.speech.downloads
    if not downloads.is_installed(model_id):
        logger.warning("fetching the speech model chosen at setup: %s", model_id)
        # Watching before starting: an answer published by start() itself is not missed.
        async with downloads.watch() as updates:
            first = downloads.start(model_id)
            state = first.state
            while state != "installed":
                if state in GIVEN_UP:
                    return
                update = await updates.get()
                if update.id == model_id:
                    state = update.state
    config = app.config
    if marker(model_id) not in config.seeded:
        await app.save_config(config.model_copy(update={"seeded": [*config.seeded, marker(model_id)]}))
