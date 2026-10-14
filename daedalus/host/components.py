"""The optional pieces of an installation: what each one unlocks, whether it is here, and how it arrives.

A portable build is small because most of what an agent *can* do, most installations never ask it to
do. The headless browser is a third of a container image; Node is another quarter; the speech engine
is fifteen megabytes of wheels that an installation which never says a word out loud should not
carry. Leaving them out is right. Leaving them out silently is not — the owner opened Settings →
Voice on a portable build and found two sentences about the browser's own synthesiser where the model
pickers should have been, with nothing on the page saying that a download would put them there.

So this module is the one list. Every optional piece, what it costs, what it turns on, how to see
whether it is present, and — the part that was missing — how to get it from inside the app, which is
a different answer in each of the two installation shapes:

============  =======================================  =========================================
component     native (a process on the machine)        Docker (a container from an image)
============  =======================================  =========================================
speech        a Python extra, synced into the           in the image already
              installation's own environment
browser       the launcher fetches it into the          the ``:browser`` image tag carries it;
              installation's runtime folder             switching tags is the launcher's job
node          the launcher fetches it, likewise         no image carries it
searxng       this process installs it into the         the stack's ``search`` profile, or the
              runtime folder; the supervisor runs it    same install into the state volume
git, rg,      the machine's own, or the pinned copy     in the image already
opus-tools    in the runtime folder
bwrap         Linux only; the machine's package         in the image already
speech models downloaded from the pickers               the same, and to the same place
============  =======================================  =========================================

Two rules the shape of this file follows from:

**A status is measured, never assumed.** ``installed`` means something was found — an import, a
binary, a directory — not that the mode it is running in usually has it. The container that was built
without the speech extra reads ``missing`` here, and says so, rather than being told what its image
normally holds.

**Nothing claims to be installable that is not.** ``installable`` is false wherever this process
cannot actually do it, and then ``fix`` carries the command that can, or the tag to run. A button
that produces an error is worse than a sentence that produces a command.
"""

from __future__ import annotations

import importlib
import importlib.util
import logging
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from daedalus.config import RuntimeConfig, Settings
from daedalus.host import searxng, toolchain

if TYPE_CHECKING:  # pragma: no cover - the installer holds a registry, so the arrow points one way
    from daedalus.host.component_install import Installer

logger = logging.getLogger(__name__)

# -- the components -----------------------------------------------------------------------

SPEECH = "speech"
BROWSER = "browser"
NODE = "node"
GIT = "git"
RIPGREP = "ripgrep"
OPUS = "opus"
BWRAP = "bwrap"
STT_MODELS = "stt-models"
TTS_VOICES = "tts-voices"
SEARXNG = "searxng"

#: Every id, in the order the page lists them: what the operator is most likely to want first.
ORDER: tuple[str, ...] = (SPEECH, STT_MODELS, TTS_VOICES, SEARXNG, BROWSER, NODE, GIT, RIPGREP, OPUS, BWRAP)

#: How a component is obtained from inside the app.
#:
#: ``extra``    a Python extra this process syncs into the environment it is running out of.
#: ``launcher`` the launcher fetches it; this process asks over the loopback bridge.
#: ``models``   a download the operator picks from a catalog, on the voice page.
#: ``service``  this process installs it into a folder of its own, and the supervisor runs it.
#: ``none``     nothing here can install it; ``fix`` says what can.
HOW = ("extra", "launcher", "models", "service", "none")


@dataclass(frozen=True, slots=True)
class Component:
    """One optional piece, as the catalogue declares it — before anything is measured.

    ``enables`` are stable keys, not sentences: the app writes them in the reader's language. The
    prose this module produces (``detail``, ``fix``) is the runtime-specific half, which depends on
    what was found and therefore cannot live in a translation table.
    """

    id: str
    enables: tuple[str, ...]
    download_bytes: int
    """Roughly what arrives over the network when it is installed. Zero where nothing is downloaded
    because the piece is either present or a matter of the machine's own package manager."""
    requires_restart: bool
    """Whether the running process picks it up by itself. The speech engine is imported on demand, so
    it does not; Node and the browser are found through the environment the supervisor was started
    with, so they do."""


CATALOGUE: dict[str, Component] = {
    SPEECH: Component(SPEECH, ("stt", "tts", "voicenotes"), 15 * 1024 * 1024, requires_restart=False),
    STT_MODELS: Component(STT_MODELS, ("stt",), 0, requires_restart=False),
    TTS_VOICES: Component(TTS_VOICES, ("tts",), 0, requires_restart=False),
    BROWSER: Component(BROWSER, ("skills.browser", "screenshots"), 316 * 1024 * 1024, requires_restart=True),
    NODE: Component(NODE, ("skills.node", "npx"), 58 * 1024 * 1024, requires_restart=True),
    GIT: Component(GIT, ("selfdev", "projects"), 0, requires_restart=False),
    RIPGREP: Component(RIPGREP, ("search",), 0, requires_restart=False),
    OPUS: Component(OPUS, ("voicenotes",), 0, requires_restart=False),
    BWRAP: Component(BWRAP, ("sandbox",), 0, requires_restart=False),
    # Six megabytes of source and about thirty-five of wheels; it occupies some 130 MB unpacked.
    SEARXNG: Component(SEARXNG, ("websearch",), 40 * 1024 * 1024, requires_restart=False),
}

#: The requirement name a skill's front matter uses, for the two components skills can declare.
SKILL_REQUIREMENTS = {BROWSER: "browser", NODE: "node"}


@dataclass(slots=True)
class Status:
    """What one component turned out to be, here, now."""

    id: str
    state: str
    """``installed``, ``missing``, ``installing`` or ``unavailable``."""
    detail: str = ""
    """One sentence of runtime fact, in English: what was found, or why this platform cannot have it.

    Kept for the places that read the answer rather than render it — the doctor, the command line,
    the reason a refusal carries. The page shows ``detail_key`` instead, because a page that is in
    Russian everywhere except its largest body line is worse than one that is not translated at all.
    """
    detail_key: str = ""
    """The same fact as a key the app writes out in the reader's language, with ``detail_args``
    filling the holes. Empty only where nothing measured has been said."""
    detail_args: dict[str, str] = field(default_factory=dict)
    installable: bool = False
    how: str = "none"
    fix: str = ""
    """The command or the image tag that puts it there, when the app cannot."""
    download_bytes: int = 0
    disk_bytes: int = 0
    """What it occupies here, where that is knowable — the speech models and voices, which are files
    in the state directory. Zero for the pieces whose size is the machine's business."""
    requires_restart: bool = False
    installed_count: int = 0
    total_count: int = 0
    """``3 of 12 installed``, for the two model components. Zero and zero for everything else."""
    skills: list[str] = field(default_factory=list)
    """The skills that declare this component in their front matter and are therefore held back."""
    enables: tuple[str, ...] = ()
    progress: dict[str, object] | None = None
    service: dict[str, object] | None = None
    """For a component that is a running process rather than files: ``starting``, ``running``,
    ``failed`` or ``stopped``, with the address it answers at and the reason it failed. None for the
    rest, and for a service that is not installed."""
    removable: bool = False

    def as_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "state": self.state,
            "detail": self.detail,
            "detail_key": self.detail_key,
            "detail_args": dict(self.detail_args),
            "installable": self.installable,
            "how": self.how,
            "fix": self.fix,
            "download_bytes": self.download_bytes,
            "disk_bytes": self.disk_bytes,
            "requires_restart": self.requires_restart,
            "installed_count": self.installed_count,
            "total_count": self.total_count,
            "skills": list(self.skills),
            "enables": list(self.enables),
            "progress": self.progress,
            "service": self.service,
            "removable": self.removable,
        }


# -- detection ----------------------------------------------------------------------------


def engine_installed() -> bool:
    """Whether the speech engine can be imported.

    Asked of the import system rather than of a package list, and asked afresh: an install that has
    just finished writes into the very environment this process imports from, and the answer has to
    change without a restart for that to be worth anything.
    """
    importlib.invalidate_caches()
    return all(importlib.util.find_spec(name) is not None for name in ("sherpa_onnx", "onnxruntime", "tokenizers"))


def _found(path: str | None) -> bool:
    return bool(path)


def _dir_bytes(root: Path) -> int:
    total = 0
    if not root.is_dir():
        return 0
    for current, _dirs, files in os.walk(root):
        for name in files:
            try:
                total += (Path(current) / name).stat().st_size
            except OSError:
                continue
    return total


def skills_needing(requirement: str, skills_dir: Path) -> list[str]:
    """The skills whose front matter declares this requirement, by folder name.

    Read from the files rather than from a running store: this is asked by the doctor and by a page
    that renders before a session exists, and a skill is a directory with a heading in it either way.
    """
    found: list[str] = []
    if not skills_dir.is_dir():
        return found
    for entry in sorted(skills_dir.iterdir()):
        card = entry / "SKILL.md"
        if not card.is_file():
            continue
        try:
            head = card.read_text(encoding="utf-8", errors="replace")[:1024]
        except OSError:
            continue
        for line in head.splitlines():
            if line.startswith("requires:"):
                names = [part.strip() for part in line.partition(":")[2].split(",")]
                if requirement in names:
                    found.append(entry.name)
                break
    return found


# -- the registry -------------------------------------------------------------------------


class Registry:
    """Every component measured against one installation.

    Built per request rather than cached: the whole point of the page is that a component appears
    while the operator is looking at it. The probes are a handful of ``which`` calls, two imports and
    a directory listing — cheap enough to repeat, and a cached "missing" on the screen after a
    successful install is the bug this replaces.
    """

    def __init__(self, settings: Settings, config: RuntimeConfig, *, installer: Installer | None = None) -> None:
        self.settings = settings
        self.config = config
        self.installer = installer

    # -- one component at a time ------------------------------------------------------

    def status(self, component_id: str) -> Status:
        component = CATALOGUE[component_id]
        probe = {
            SPEECH: self._speech,
            STT_MODELS: self._stt_models,
            TTS_VOICES: self._tts_voices,
            BROWSER: self._browser,
            NODE: self._node,
            GIT: self._binary_status(GIT, "git", "git"),
            RIPGREP: self._binary_status(RIPGREP, "rg", "ripgrep"),
            OPUS: self._binary_status(OPUS, "opusdec", "opus-tools"),
            BWRAP: self._bwrap,
            SEARXNG: self._searxng,
        }[component_id]
        status = probe()
        status.enables = component.enables
        status.download_bytes = component.download_bytes
        status.requires_restart = component.requires_restart
        if requirement := SKILL_REQUIREMENTS.get(component_id):
            if status.state != "installed":
                status.skills = skills_needing(requirement, self.settings.skills_dir)
        if self.installer is not None:
            running = self.installer.progress_of(component_id)
            if running is not None:
                status.progress = running
                if running.get("state") in ("queued", "running"):
                    status.state = "installing"
        return status

    def view(self) -> dict[str, object]:
        """The whole page: every component, the shape of the installation, and what it all occupies."""
        entries = [self.status(cid) for cid in ORDER]
        return {
            "mode": "native" if self.settings.native else "docker",
            "launcher": self.installer.launcher_present() if self.installer is not None else False,
            "components": [entry.as_dict() for entry in entries],
            "disk_bytes": sum(entry.disk_bytes for entry in entries),
            "missing": [entry.id for entry in entries if entry.state == "missing"],
            "busy": self.installer.running_id() if self.installer is not None else "",
        }

    def summary(self) -> dict[str, object]:
        """The short form that rides on ``/api/capabilities``, so the shell can badge the tab.

        ``needed`` is the sharp end: a component that something *already configured* wants and does
        not have. A local voice chosen with no engine to run it is the case the owner hit — the page
        looked configured and said nothing.
        """
        entries = {cid: self.status(cid) for cid in ORDER}
        missing = [cid for cid, entry in entries.items() if entry.state == "missing"]
        needed: list[str] = []
        if entries[SPEECH].state == "missing" and (self.config.stt.local_model or self.config.companion.tts.local_voice):
            needed.append(SPEECH)
        if entries[OPUS].state == "missing" and self.settings.telegram_bot_token:
            needed.append(OPUS)
        return {
            "mode": "native" if self.settings.native else "docker",
            "missing": missing,
            "needed": needed,
            "installing": self.installer.running_id() if self.installer is not None else "",
        }

    # -- the probes -------------------------------------------------------------------

    def _speech(self) -> Status:
        if engine_installed():
            return Status(SPEECH, "installed", "the speech engine is importable", detail_key="comp.detail.speech.ok")
        if self.settings.native:
            if shutil.which("uv") is None:
                return Status(SPEECH, "missing", "uv is not on the PATH, so the extra cannot be synced from here",
                              detail_key="comp.detail.speech.nouv", fix="uv sync --frozen --inexact --extra speech")
            return Status(SPEECH, "missing", "the speech extra is not in this environment", detail_key="comp.detail.speech.missing",
                          installable=True, how="extra", fix="uv sync --frozen --inexact --extra speech")
        return Status(SPEECH, "missing", "this image was built without the speech extra", detail_key="comp.detail.speech.image",
                      fix="run the published image, which carries it")

    def _stt_models(self) -> Status:
        return self._models("stt", STT_MODELS)

    def _tts_voices(self) -> Status:
        return self._models("tts", TTS_VOICES)

    def _models(self, kind: str, component_id: str) -> Status:
        """A model component is never installed or missing as a whole: it is a count and a size.

        Which is why it is here at all — the disk the voice pickers have taken is the number an
        operator looks for when a portable installation stops being small, and it was on no page.
        """
        from daedalus.speech import catalog as stt_catalog  # Lazy: answers without the speech extra
        from daedalus.speech import tts_catalog  # Lazy: same

        catalogue = stt_catalog.MODELS if kind == "stt" else tts_catalog.VOICES
        root = self.settings.state_dir / "models" / kind
        installed = [entry for entry in catalogue if (root / entry.id).is_dir()]
        state = "installed" if installed else "missing"
        detail = f"{len(installed)} of {len(catalogue)} downloaded" if installed else "none downloaded yet"
        return Status(
            component_id, state, detail,
            detail_key="comp.detail.models.some" if installed else "comp.detail.models.none",
            detail_args={"n": str(len(installed)), "total": str(len(catalogue))},
            installable=False, how="models",
            # Not a button here and not a command either: one model at a time is a choice, made in
            # front of the catalogue that says what each one speaks and what it costs.
            fix="choose one in Settings → Voice",
            disk_bytes=_dir_bytes(root), installed_count=len(installed), total_count=len(catalogue),
        )

    def _browser(self) -> Status:
        if toolchain.status("browser") == "ok":
            return Status(BROWSER, "installed", "Playwright, a headless Chromium and Pillow are here", detail_key="comp.detail.browser.ok")
        if self.settings.native:
            return Status(BROWSER, "missing", "the browser extra and the headless shell are not in this installation",
                          detail_key="comp.detail.browser.missing", installable=True, how="launcher",
                          fix="uv sync --extra browser && python -m playwright install chromium")
        return Status(BROWSER, "missing", "this image tag leaves the browser out", detail_key="comp.detail.browser.image",
                      fix="run the :browser tag of the agent image")

    def _node(self) -> Status:
        if toolchain.status("node") == "ok":
            return Status(NODE, "installed", "node and npx are on the PATH", detail_key="comp.detail.node.ok")
        if self.settings.native:
            return Status(NODE, "missing", "Node is not in this installation's runtime folder", detail_key="comp.detail.node.missing",
                          installable=True, how="launcher", fix="install Node and put it on the PATH")
        return Status(NODE, "unavailable", "no published image carries Node; a native installation fetches it on demand",
                      detail_key="comp.detail.node.image", fix="run this installation natively, or install Node in a derived image")

    def _binary_status(self, component_id: str, binary: str, package: str):  # type: ignore[no-untyped-def]
        def probe() -> Status:
            where = shutil.which(binary)
            if _found(where):
                return Status(component_id, "installed", f"{binary} is at {where}",
                              detail_key="comp.detail.binary.ok", detail_args={"binary": binary, "path": str(where)})
            if self.settings.native:
                return Status(component_id, "missing", f"{binary} is not on the PATH",
                              detail_key="comp.detail.binary.nopath", detail_args={"binary": binary},
                              fix=f"install {package} with this machine's package manager")
            return Status(component_id, "missing", f"{binary} is not in this image",
                          detail_key="comp.detail.binary.noimage", detail_args={"binary": binary},
                          fix=f"add {package} to deploy/apt-packages.txt and rebuild")

        return probe

    def _searxng(self) -> Status:
        root = searxng.root_for(self.settings.state_dir, self.settings.runtime_dir)
        running = searxng.service(root)
        if running is not None:
            key = f"comp.detail.searxng.{running['state']}"
            detail = f"SearXNG {running['version']} is {running['state']}" + (f" at {running['url']}" if running["url"] else "")
            if running["error"]:
                detail += f": {running['error']}"
            return Status(SEARXNG, "installed", detail, detail_key=key,
                          detail_args={"url": str(running["url"]), "error": str(running["error"]), "seconds": str(running["retry_in"])},
                          how="service", disk_bytes=_dir_bytes(root), service=running, removable=True)
        configured = self.config.tools.web.search.searxng.url
        if not self.settings.native and searxng.is_default_url(configured) and searxng.stack_answers(configured):
            # The compose stack's own SearXNG, from the ``search`` profile. Installed in the sense
            # that matters — WebSearch reaches it — and not this process's to remove.
            return Status(SEARXNG, "installed", f"the stack's SearXNG answers at {configured}", detail_key="comp.detail.searxng.stack",
                          detail_args={"url": configured}, how="none",
                          service={"state": "running", "url": configured, "error": "", "retry_in": 0, "version": ""})
        if not searxng.is_default_url(configured):
            return Status(SEARXNG, "missing", f"WebSearch is pointed at {configured}, set by hand; an install here would not change that",
                          detail_key="comp.detail.searxng.custom", detail_args={"url": configured}, installable=True, how="service")
        return Status(SEARXNG, "missing", "no SearXNG is installed; WebSearch asks DuckDuckGo directly",
                      detail_key="comp.detail.searxng.missing", installable=True, how="service")

    def _bwrap(self) -> Status:
        from daedalus.tools.shell import bwrap_status  # Lazy: the probe is a subprocess, cached

        answer = bwrap_status()
        if answer == "ok":
            return Status(BWRAP, "installed", "bubblewrap can create the namespaces the sandbox needs", detail_key="comp.detail.bwrap.ok")
        if not sys.platform.startswith("linux"):
            return Status(BWRAP, "unavailable", answer, detail_key="comp.detail.bwrap.platform", detail_args={"platform": sys.platform})
        if shutil.which("bwrap") is None:
            return Status(BWRAP, "missing", answer, detail_key="comp.detail.bwrap.missing",
                          fix="install bubblewrap with this machine's package manager")
        # Installed and refused: a kernel that forbids unprivileged user namespaces, or a seccomp
        # profile in the way. Nothing to install; the fix is a setting on the machine.
        return Status(BWRAP, "unavailable", answer, detail_key="comp.detail.bwrap.refused",
                      fix="allow unprivileged user namespaces on this machine")


__all__ = [
    "BROWSER",
    "BWRAP",
    "CATALOGUE",
    "GIT",
    "HOW",
    "NODE",
    "OPUS",
    "ORDER",
    "RIPGREP",
    "SEARXNG",
    "SPEECH",
    "STT_MODELS",
    "TTS_VOICES",
    "Component",
    "Registry",
    "Status",
    "engine_installed",
    "skills_needing",
]
