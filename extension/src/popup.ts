// The popup: whether this Chrome is connected to Daedalus and to which installation, the check the
// operator can run, the agent's groups with their controls, and the connection settings. It holds no
// state of its own; it asks the worker and draws the answer, once a second while open so that "last
// heartbeat" stays true.

import { isKey, language, t, type Key } from "./i18n.js";
import type { CheckOutcome, Status } from "./relay.js";

const $ = <T extends HTMLElement = HTMLElement>(id: string) => document.getElementById(id) as T;

function ask<T>(message: Record<string, unknown>): Promise<T> {
  return chrome.runtime.sendMessage(message) as Promise<T>;
}

function translateStatic(): void {
  document.documentElement.lang = language();
  for (const element of document.querySelectorAll<HTMLElement>("[data-i18n]")) {
    const key = element.dataset.i18n ?? "";
    if (isKey(key)) element.textContent = t(key);
  }
}

function ago(now: number, at?: number): string {
  if (at === undefined) return t("ago.never");
  const seconds = Math.max(0, Math.round((now - at) / 1000));
  if (seconds < 2) return t("ago.now");
  if (seconds < 120) return t("ago.seconds", { n: seconds });
  return t("ago.minutes", { n: Math.round(seconds / 60) });
}

let lastNoticeAt = 0;
let editing = false;

function render(status: Status): void {
  const phase = $("phase");
  phase.dataset.phase = status.phase;
  $("phase-text").textContent = t(`phase.${status.phase}` as Key);

  const error = $("error");
  error.hidden = !status.error;
  if (status.error) error.textContent = t(`error.${status.error}` as Key, { name: status.errorName ?? "?" });

  $("transport").textContent =
    status.mode === "native"
      ? t("transport.native")
      : status.mode === "server"
        ? t("transport.server", { server: (status.server ?? "").replace(/^https?:\/\//, "") })
        : t("transport.off");
  $("install").textContent = status.installName ?? "—";
  $("version").textContent = status.daemonVersion ?? "—";
  $("heartbeat").textContent = status.phase === "connected" ? ago(status.now, status.lastHeartbeat) : "—";
  $("tabs").textContent = t("tabs.count", { tabs: status.tabs, groups: status.groups.length });

  if (status.notice && status.notice.at !== lastNoticeAt) showNotice(status.notice);

  $("groups-empty").hidden = status.groups.length > 0;
  const list = $("groups");
  list.replaceChildren(
    ...status.groups.map((group) => {
      const item = document.createElement("li");
      const title = document.createElement("div");
      title.className = "title";
      title.textContent = group.title || t("group.untitled");
      const meta = document.createElement("div");
      meta.className = "meta";
      meta.textContent = group.paused ? t("group.paused") : t("group.tabs", { n: group.tabs });
      const buttons = document.createElement("div");
      buttons.className = "buttons";
      const toggle = document.createElement("button");
      toggle.textContent = group.paused ? t("group.resume") : t("group.pause");
      toggle.onclick = () => void ask({ kind: "pause", group: group.id, paused: !group.paused }).then(refresh);
      const stop = document.createElement("button");
      stop.className = "danger";
      stop.textContent = t("group.stop");
      stop.onclick = () => void ask({ kind: "stop", group: group.id }).then(refresh);
      buttons.append(toggle, stop);
      item.append(title, meta, buttons);
      return item;
    }),
  );

  if (!editing) {
    $<HTMLInputElement>("name").value = status.profileName;
    if (status.server) $<HTMLInputElement>("server").value = status.server.replace(/^https:\/\//, "");
  }
  $<HTMLInputElement>("window-mode").checked = status.windowMode === "current";
  $("use-native").hidden = status.mode === "native";
  $("disconnect").hidden = status.mode === "off";
}

function showNotice(notice: { at: number; text: string }): void {
  lastNoticeAt = notice.at;
  const element = $("notice");
  element.textContent = notice.text ? `${t("notice.checked")}: ${notice.text}` : t("notice.checked");
  element.hidden = false;
  // Restart the flash even when the previous one is still showing.
  element.style.animation = "none";
  void element.offsetWidth;
  element.style.animation = "";
  setTimeout(() => {
    if (lastNoticeAt === notice.at) element.hidden = true;
  }, 6000);
}

async function refresh(): Promise<void> {
  render(await ask<Status>({ kind: "get-status" }));
}

function wire(): void {
  $("check").onclick = async () => {
    const result = $("check-result");
    result.removeAttribute("data-ok");
    result.textContent = t("check.waiting");
    const outcome = await ask<CheckOutcome>({ kind: "check" });
    result.dataset.ok = String(outcome.ok);
    result.textContent = outcome.ok
      ? t("check.ok", { ms: outcome.ms })
      : outcome.reason === "timeout"
        ? t("check.timeout")
        : t("check.offline");
  };
  $("pair").onclick = async () => {
    const outcome = await ask<{ ok: boolean; error?: string }>({
      kind: "pair",
      server: $<HTMLInputElement>("server").value,
      code: $<HTMLInputElement>("code").value,
    });
    if (outcome.ok) $<HTMLInputElement>("code").value = "";
    else {
      const error = $("error");
      error.hidden = false;
      error.textContent = t(`error.${outcome.error}` as Key, { name: "" });
    }
    editing = false;
    if (outcome.ok) await refresh();
  };
  $("use-native").onclick = () => void ask({ kind: "use-native" }).then(refresh);
  $("disconnect").onclick = () => void ask({ kind: "disconnect" }).then(refresh);
  $("save-name").onclick = () => {
    editing = false;
    void ask({ kind: "set-name", name: $<HTMLInputElement>("name").value }).then(refresh);
  };
  $<HTMLInputElement>("window-mode").onchange = (event) =>
    void ask({ kind: "set-window-mode", mode: (event.target as HTMLInputElement).checked ? "current" : "own" });
  for (const id of ["name", "server", "code"]) $(id).addEventListener("input", () => (editing = true));

  chrome.runtime.onMessage.addListener((message) => {
    if (message?.kind === "notice") showNotice(message.notice);
    if (message?.kind === "status") void refresh();
  });
}

translateStatic();
wire();
void refresh();
setInterval(() => void refresh(), 1000);
