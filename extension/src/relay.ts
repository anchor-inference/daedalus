// The relay: one connection to a daemon, the tabs it may drive, and the proxy between the two. The
// service worker builds one of these and feeds it Chrome's events; the side panel asks it for its
// status, its buttons and the conversation that owns a tab. Everything that must survive the worker being stopped lives in storage (settings
// in `local`, scope in `session`); everything else is rebuilt on start.

import { refusal } from "./allowlist.js";
import { updateGuard } from "./guard.js";
import { t, type Key } from "./i18n.js";
import {
  PROTO,
  RESULT_ERRORS,
  parseDaemonFrame,
  parseSession,
  sessionName,
  type CallFrame,
  type CdpFrame,
  type DaemonFrame,
  type ExtensionFrame,
  type PanelEvent,
  type PanelGrant,
  type TabInfo,
} from "./protocol.js";
import { Scope, type Group, type Member } from "./scope.js";
import {
  normaliseServer,
  openTransport,
  pairUrl,
  relayUrl,
  type CloseReason,
  type Transport,
  type TransportFactory,
} from "./transport.js";

export type Mode = "native" | "server" | "off";
export type Phase = "idle" | "connecting" | "connected" | "error";
export type ErrorCode =
  | CloseReason
  | "version_extension"
  | "version_daemon"
  | "replaced"
  | "shutdown"
  | "unavailable"
  | "bad_server"
  | "pair_failed";

export type Settings = {
  mode: Mode;
  server?: string;
  deviceToken?: string;
  profileId: string;
  profileName: string;
  windowMode: "own" | "current";
};

export type Status = {
  mode: Mode;
  server?: string;
  phase: Phase;
  error?: ErrorCode;
  errorName?: string;
  installName?: string;
  installKind?: "native" | "server";
  daemonVersion?: string;
  lastHeartbeat?: number;
  connectedSince?: number;
  profileName: string;
  extVersion: string;
  windowMode: "own" | "current";
  tabs: number;
  groups: { id: string; title: string; tabs: number; paused: boolean }[];
  notice?: { at: number; text: string };
  // How many conversations using this Chrome wait for the operator, as the host last said.
  waiting: number;
  now: number;
};

/** The conversation whose agent group holds a tab, as the side panel follows the active tab. */
export type TabOwner = { group: string; session?: string; title: string; paused: boolean };

export type CheckOutcome = { ok: true; ms: number } | { ok: false; reason: "timeout" | "offline" };

// The daemon pings every 15 s and three missed pongs end the relay on its side; this side gives up a
// little later than that, so the two do not race each other into a reconnect.
export const HEARTBEAT_TIMEOUT_MS = 50_000;
export const CHECK_TIMEOUT_MS = 5_000;
// Reconnect delays, in order, then the last one forever. A missing registration or a busy relay does
// not fix itself in a second, so those wait longer instead of spawning a bridge every second.
const RECONNECT_MS = [1_000, 2_000, 5_000, 10_000, 30_000];
const SLOW_RECONNECT_MS: Partial<Record<ErrorCode, number>> = {
  not_registered: 30_000,
  busy: 60_000,
  version_extension: 300_000,
  version_daemon: 300_000,
};
// Reattach after a detach that was not the tab closing: the infobar's Cancel, or the operator opening
// DevTools on the tab. The delays are long enough that a person who meant it can see it happen and
// close the group instead, and the attempts end so that a tab the operator keeps taking back is let go.
export const REATTACH_MS = [2_000, 5_000, 15_000, 30_000, 60_000];
// Trusted input within this long after the daemon dispatched input into the tab is taken for the
// daemon's own: `Input.dispatch*` events arrive in the page as trusted too.
export const OWN_INPUT_MS = 1_000;
// One `input` frame per tab per this long is enough to keep the daemon's human-control lease alive.
export const INPUT_REPORT_MS = 1_000;
// While the operator holds a group paused, its tabs are reported as the operator's input this often,
// which keeps the daemon's human-control lease from running out for as long as the pause lasts.
export const PAUSE_RENEW_MS = 2_000;
const NOTICE_SHOW_MS = 10_000;
const FLASH_MS = 3_000;
const MENU_ROOT = "daedalus-grant";
// "Ask Daedalus about this page": opens the side panel on a new conversation. The worker answers its
// click itself, at once, because opening the panel needs the click's gesture.
export const MENU_ASK = "daedalus-ask";
const GRANT_KEY = "panelGrant";
// A notification's id carries what its click needs, since the worker that shows it may be stopped
// and started again before the click: the conversation and the agent's tab.
export const NOTICE_PREFIX = "daedalus-panel";

const COLOURS = { connected: "#1a7f37", idle: "#6e7781", error: "#cf222e", flash: "#1f6feb", waiting: "#bf8700" };

type Pending = { resolve: (outcome: CheckOutcome) => void; sent: number; timer: ReturnType<typeof setTimeout> };

export class Relay {
  readonly scope = new Scope();
  settings: Settings = { mode: "native", profileId: "", profileName: "Chrome", windowMode: "own" };
  private transport: Transport | null = null;
  private phase: Phase = "idle";
  private error?: ErrorCode;
  private errorName?: string;
  private welcome?: { install_name: string; install_kind: "native" | "server"; daemon_version: string };
  private lastHeartbeat?: number;
  private connectedSince?: number;
  private attempt = 0;
  private reconnectTimer?: ReturnType<typeof setTimeout>;
  private heartbeatTimer?: ReturnType<typeof setInterval>;
  private checkSeq = 0;
  private checks = new Map<number, Pending>();
  private notice?: { at: number; text: string };
  private flashUntil = 0;
  private flashTimer?: ReturnType<typeof setTimeout>;
  private reattachTimers = new Map<number, ReturnType<typeof setTimeout>>();
  private reattachAttempts = new Map<number, number>();
  private lastDispatch = new Map<number, number>();
  private lastInputReport = new Map<number, number>();
  private pauseTimer?: ReturnType<typeof setInterval>;
  // Page.downloadWillBegin names a download by a DevTools guid; chrome.downloads knows it by its own
  // id. What the event said (the address and when) is how the one is found from the other.
  private downloads = new Map<string, { url: string; at: number }>();
  private waiting = 0;
  /** The windows whose side panel is open now; the worker keeps it from the panels' ports. */
  readonly openPanels = new Set<number>();

  constructor(
    private readonly openTransportFor: TransportFactory = openTransport,
    private readonly now: () => number = () => Date.now(),
  ) {}

  async start(): Promise<void> {
    const stored = await chrome.storage.local.get(["mode", "server", "deviceToken", "profileId", "profileName", "windowMode"]);
    let profileId = stored.profileId as string | undefined;
    if (!profileId) {
      profileId = crypto.randomUUID();
      await chrome.storage.local.set({ profileId });
    }
    this.settings = {
      mode: (stored.mode as Mode | undefined) ?? "native",
      server: stored.server,
      deviceToken: stored.deviceToken,
      profileId,
      profileName: (stored.profileName as string | undefined) || "Chrome",
      windowMode: stored.windowMode === "current" ? "current" : "own",
    };
    await this.scope.load();
    const session = await chrome.storage.session.get(["notice", "panelWaiting"]);
    this.notice = session.notice;
    this.waiting = typeof session.panelWaiting === "number" ? session.panelWaiting : 0;
    // A worker that was stopped lost its debugger sessions with it only if the extension reloaded;
    // either way the attach below is what makes a remembered tab drivable again, and a tab that no
    // longer exists is forgotten.
    for (const member of [...this.scope.members.values()]) {
      try {
        await chrome.tabs.get(member.tab);
      } catch {
        this.scope.remove(member.tab);
        continue;
      }
      await this.attach(member);
    }
    await this.scope.save();
    await this.refreshScopeViews();
    this.connect();
  }

  // ---- connection ----

  connect(): void {
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = undefined;
    if (this.transport) return;
    const { mode, server, deviceToken } = this.settings;
    if (mode === "off" || (mode === "server" && (!server || !deviceToken))) {
      this.setPhase("idle");
      return;
    }
    this.setPhase("connecting");
    const transport = this.openTransportFor(
      mode === "native" ? { kind: "native" } : { kind: "server", relayUrl: relayUrl(server!) },
      {
        onFrame: (frame) => {
          if (this.transport === transport) void this.onFrame(frame);
        },
        onClose: (reason, detail) => {
          if (this.transport !== transport) return;
          this.transport = null;
          this.lost(reason, reason === "busy" ? detail : undefined);
        },
      },
    );
    this.transport = transport;
    void this.sendHello(transport);
  }

  private async sendHello(transport: Transport): Promise<void> {
    const platform = await chrome.runtime.getPlatformInfo().then((info) => info.os, () => "unknown");
    const hello: ExtensionFrame = {
      t: "hello",
      proto: PROTO,
      ext_version: chrome.runtime.getManifest().version,
      chrome_version: chromeVersion(),
      platform,
      profile_id: this.settings.profileId,
      profile_name: this.settings.profileName,
      ...(this.settings.mode === "server" && this.settings.deviceToken ? { token: this.settings.deviceToken } : {}),
    };
    transport.send(hello);
  }

  private send(frame: ExtensionFrame): void {
    this.transport?.send(frame);
  }

  /** Ends the current connection on purpose (settings changed); no error is shown. */
  private hangUp(): void {
    const transport = this.transport;
    this.transport = null;
    transport?.close();
    this.stopHeartbeat();
    this.welcome = undefined;
    this.connectedSince = undefined;
    this.failChecks();
  }

  private lost(reason: ErrorCode, name?: string): void {
    const wasConnected = this.phase === "connected";
    this.hangUp();
    this.error = reason === "not_running" && wasConnected ? "lost" : reason;
    this.errorName = name;
    this.setPhase("error");
    if (reason === "pairing_expired") return;
    const delay = SLOW_RECONNECT_MS[reason] ?? RECONNECT_MS[Math.min(this.attempt, RECONNECT_MS.length - 1)];
    this.attempt += 1;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = setTimeout(() => this.connect(), delay);
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();
    this.heartbeatTimer = setInterval(() => {
      if (this.phase === "connected" && this.now() - (this.lastHeartbeat ?? 0) > HEARTBEAT_TIMEOUT_MS) {
        this.lost("lost");
      }
    }, 5_000);
  }

  private stopHeartbeat(): void {
    clearInterval(this.heartbeatTimer);
    this.heartbeatTimer = undefined;
  }

  /** Called on the worker's alarm: a worker Chrome restarted must not wait for a timer it lost. */
  wake(): void {
    if (!this.transport && !this.reconnectTimer && this.error !== "pairing_expired") this.connect();
  }

  private async onFrame(raw: unknown): Promise<void> {
    const frame = parseDaemonFrame(raw);
    if (!frame) return;
    switch (frame.t) {
      case "welcome":
        return this.onWelcome(frame);
      case "ping":
        this.lastHeartbeat = this.now();
        this.send({ t: "pong", n: frame.n });
        this.broadcast();
        return;
      case "check_ok": {
        const pending = this.checks.get(frame.n);
        if (!pending) return;
        this.checks.delete(frame.n);
        clearTimeout(pending.timer);
        pending.resolve({ ok: true, ms: this.now() - pending.sent });
        return;
      }
      case "bye":
        return this.onBye(frame);
      case "cdp":
        return this.onCdp(frame);
      case "call":
        return this.onCall(frame);
    }
  }

  private onWelcome(frame: Extract<DaemonFrame, { t: "welcome" }>): void {
    if (frame.proto !== PROTO) {
      this.lost(frame.proto > PROTO ? "version_extension" : "version_daemon");
      return;
    }
    this.welcome = frame;
    this.attempt = 0;
    this.error = undefined;
    this.errorName = undefined;
    this.lastHeartbeat = this.now();
    this.connectedSince = this.now();
    this.setPhase("connected");
    this.startHeartbeat();
    // A daemon that restarted knows nothing of the tabs this extension still holds; telling it they
    // are joined lets it take them back or close their group, instead of leaving them orphaned.
    for (const member of this.scope.members.values()) void this.report("joined", member);
    this.renewPauses();
  }

  private onBye(frame: Extract<DaemonFrame, { t: "bye" }>): void {
    const reasons: Record<string, ErrorCode> = {
      shutdown: "shutdown",
      replaced: "replaced",
      another_relay: "busy",
      proto: (frame.proto ?? 0) > PROTO ? "version_extension" : "version_daemon",
      lost: "lost",
      bad_frame: "lost",
      not_running: "not_running",
      unavailable: "unavailable",
      unauthorized: "pairing_expired",
    };
    // The other Chrome's name is the end of the daemon's message ("…: <name>"); the panel shows it.
    const name = frame.reason === "another_relay" ? /:\s*(.+)$/.exec(frame.message ?? "")?.[1] : undefined;
    this.lost(reasons[frame.reason] ?? "lost", name);
  }

  // ---- the side panel's buttons ----

  status(): Status {
    const groups = [...this.scope.groups.values()].map((group) => ({
      id: group.id,
      title: group.title,
      tabs: this.scope.tabsOf(group.id).length,
      paused: group.paused,
    }));
    return {
      mode: this.settings.mode,
      server: this.settings.server,
      phase: this.phase,
      error: this.error,
      errorName: this.errorName,
      installName: this.welcome?.install_name,
      installKind: this.welcome?.install_kind,
      daemonVersion: this.welcome?.daemon_version,
      lastHeartbeat: this.lastHeartbeat,
      connectedSince: this.connectedSince,
      profileName: this.settings.profileName,
      extVersion: chrome.runtime.getManifest().version,
      windowMode: this.settings.windowMode,
      tabs: this.scope.members.size,
      groups,
      notice: this.notice && this.now() - this.notice.at < NOTICE_SHOW_MS ? this.notice : undefined,
      waiting: this.waiting,
      now: this.now(),
    };
  }

  check(): Promise<CheckOutcome> {
    if (this.phase !== "connected") return Promise.resolve({ ok: false, reason: "offline" });
    const n = ++this.checkSeq;
    return new Promise((resolve) => {
      const timer = setTimeout(() => {
        this.checks.delete(n);
        resolve({ ok: false, reason: "timeout" });
      }, CHECK_TIMEOUT_MS);
      this.checks.set(n, { resolve, sent: this.now(), timer });
      this.send({ t: "check", n });
    });
  }

  private failChecks(): void {
    for (const pending of this.checks.values()) {
      clearTimeout(pending.timer);
      pending.resolve({ ok: false, reason: "offline" });
    }
    this.checks.clear();
  }

  async pause(groupId: string, paused: boolean): Promise<void> {
    const group = this.scope.groups.get(groupId);
    if (!group || group.paused === paused) return;
    group.paused = paused;
    await this.scope.save();
    this.renewPauses();
    this.broadcast();
  }

  /**
   * Keeps a paused group the operator's. The protocol has no pause of its own; what it has is the
   * operator's input, which makes the daemon hand control to the human for a short lease. Reporting
   * input on every tab of a paused group, now and then every couple of seconds, holds that lease for
   * as long as the pause lasts, and the daemon's input is refused here meanwhile as well.
   */
  private renewPauses(): void {
    clearInterval(this.pauseTimer);
    this.pauseTimer = undefined;
    const paused = () => [...this.scope.members.values()].filter((m) => this.scope.groups.get(m.group)?.paused);
    if (!paused().length) return;
    const renew = () => {
      for (const member of paused()) this.send({ t: "input", tab: member.tab });
    };
    renew();
    this.pauseTimer = setInterval(renew, PAUSE_RENEW_MS);
  }

  /** The panel's "Stop and close": the group's tabs are closed and the daemon is told it is over. */
  async stop(groupId: string): Promise<void> {
    const group = this.scope.groups.get(groupId);
    if (!group) return;
    await this.closeGroup(group);
    this.send({ t: "tabs", op: "group_closed", group: group.id });
  }

  async pair(serverInput: string, code: string): Promise<{ ok: true } | { ok: false; error: ErrorCode }> {
    const origin = normaliseServer(serverInput);
    if (!origin) return { ok: false, error: "bad_server" };
    let token: string | undefined;
    try {
      const response = await fetch(pairUrl(origin), {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          code: code.trim(),
          profile_id: this.settings.profileId,
          profile_name: this.settings.profileName,
          ext_version: chrome.runtime.getManifest().version,
          chrome_version: chromeVersion(),
        }),
      });
      if (!response.ok) return { ok: false, error: response.status >= 500 ? "server_unreachable" : "pair_failed" };
      const body = (await response.json()) as { token?: unknown };
      if (typeof body.token === "string" && body.token) token = body.token;
    } catch {
      return { ok: false, error: "server_unreachable" };
    }
    if (!token) return { ok: false, error: "pair_failed" };
    await this.applySettings({ mode: "server", server: origin, deviceToken: token });
    return { ok: true };
  }

  async useNative(): Promise<void> {
    await this.applySettings({ mode: "native" });
  }

  /** "Disconnect": stops connecting and forgets a server's device token. */
  async disconnect(): Promise<void> {
    await this.applySettings({ mode: "off", deviceToken: undefined });
  }

  async setProfileName(name: string): Promise<void> {
    await this.applySettings({ profileName: name.trim() || "Chrome" });
  }

  async setWindowMode(windowMode: "own" | "current"): Promise<void> {
    this.settings.windowMode = windowMode;
    await chrome.storage.local.set({ windowMode });
    this.broadcast();
  }

  private async applySettings(change: Partial<Settings>): Promise<void> {
    const before = `${this.settings.mode} ${this.settings.server ?? ""}`;
    this.settings = { ...this.settings, ...change };
    // The panel's credential is the installation's it came from: another one, or none, must not be
    // framed with it.
    if (`${this.settings.mode} ${this.settings.server ?? ""}` !== before) await this.forgetGrant();
    const { mode, server, deviceToken, profileName } = this.settings;
    await chrome.storage.local.set({ mode, profileName, ...(server ? { server } : {}), ...(deviceToken ? { deviceToken } : {}) });
    if (!deviceToken) await chrome.storage.local.remove("deviceToken");
    this.hangUp();
    this.error = undefined;
    this.errorName = undefined;
    this.attempt = 0;
    this.connect();
  }

  // ---- DevTools commands and events ----

  private async onCdp(frame: CdpFrame): Promise<void> {
    const refuse = (code: string, message: string) =>
      this.send({ t: "result", id: frame.id, ok: false, error: { code, message } });
    const target = parseSession(frame.session);
    if (!target) return refuse(RESULT_ERRORS.refused, "only a tab's own session can be addressed");
    const why = refusal(frame.method);
    if (why) return refuse(RESULT_ERRORS.refused, why);
    const member = this.scope.members.get(target.tabId);
    if (!member) return refuse(RESULT_ERRORS.refused, "the tab is outside the agent's groups");
    if (target.child && !member.children.includes(target.child)) return refuse(RESULT_ERRORS.refused, "unknown child session");
    if (!member.attached) return refuse(RESULT_ERRORS.detached, "the debugger is not attached to this tab");
    const group = this.scope.groups.get(member.group);
    if (frame.method.startsWith("Input.")) {
      // A paused group is the operator's: the daemon is told so by `tabs updated`, and its input is
      // refused here too, so that a daemon that missed the frame still cannot drive the tab.
      if (group?.paused) return refuse(RESULT_ERRORS.refused, "the operator paused this group");
      this.lastDispatch.set(member.tab, this.now());
    }
    let params = frame.params ?? {};
    if (frame.method.startsWith("Input.") && !target.child) void this.cursor(member.tab, frame.method, params);
    // Child sessions reach the extension only in flat mode; a nested session would be invisible to it.
    if (frame.method === "Target.setAutoAttach") params = { ...params, flatten: true };
    const screenshot = frame.method === "Page.captureScreenshot" && !target.child;
    if (screenshot) await this.border(member.tab, false);
    try {
      const value = await chrome.debugger.sendCommand(
        target.child ? { tabId: member.tab, sessionId: target.child } : { tabId: member.tab },
        frame.method,
        params,
      );
      this.send({ t: "result", id: frame.id, ok: true, value: value ?? {} });
    } catch (error) {
      refuse(RESULT_ERRORS.cdp, errorText(error));
    } finally {
      if (screenshot) void this.border(member.tab, true);
    }
  }

  onDebuggerEvent(source: chrome.debugger.DebuggerSession, method: string, params?: object): void {
    if (source.tabId === undefined) return;
    const member = this.scope.members.get(source.tabId);
    if (!member) return;
    const p = (params ?? {}) as Record<string, unknown>;
    if (method === "Target.attachedToTarget" && typeof p.sessionId === "string") {
      if (!member.children.includes(p.sessionId)) member.children.push(p.sessionId);
      void this.scope.save();
    } else if (method === "Target.detachedFromTarget" && typeof p.sessionId === "string") {
      member.children = member.children.filter((sid) => sid !== p.sessionId);
      void this.scope.save();
    } else if ((method === "Page.downloadWillBegin" || method === "Browser.downloadWillBegin") && typeof p.guid === "string") {
      this.downloads.set(p.guid, { url: String(p.url ?? ""), at: this.now() });
    }
    this.send({ t: "event", session: sessionName(member.tab, source.sessionId), method, params: params ?? {} });
  }

  onDebuggerDetach(source: chrome.debugger.Debuggee, reason: string): void {
    if (source.tabId === undefined) return;
    const member = this.scope.members.get(source.tabId);
    if (!member || !member.attached) return;
    this.markDetached(member, reason);
    if (reason !== "target_closed") this.scheduleReattach(member.tab);
    this.broadcast();
  }

  /**
   * Tells the daemon a tab's sessions are gone, in the events it already understands: a
   * `Target.detachedFromTarget` for each child, then `Inspector.detached` for the tab. Chrome sends
   * neither when the extension detaches on its own, nor for the children when the tab's session
   * ends, and a daemon left waiting on a session that no longer exists waits until its timeout.
   */
  private markDetached(member: Member, reason: string): void {
    const session = sessionName(member.tab);
    for (const child of member.children) {
      this.send({ t: "event", session, method: "Target.detachedFromTarget", params: { sessionId: child } });
    }
    this.send({ t: "event", session, method: "Inspector.detached", params: { reason } });
    member.children = [];
    member.attached = false;
    void this.scope.save();
  }

  private scheduleReattach(tab: number): void {
    const attempts = this.reattachAttempts.get(tab) ?? 0;
    clearTimeout(this.reattachTimers.get(tab));
    if (attempts >= REATTACH_MS.length) {
      this.reattachTimers.delete(tab);
      this.reattachAttempts.delete(tab);
      void this.leave(tab, "left");
      return;
    }
    this.reattachAttempts.set(tab, attempts + 1);
    this.reattachTimers.set(
      tab,
      setTimeout(async () => {
        this.reattachTimers.delete(tab);
        const member = this.scope.members.get(tab);
        if (!member || member.attached) return;
        if (await this.attach(member)) {
          // A fresh session on a tab the daemon already holds is, to the daemon, a tab joining again:
          // it adopts it as it would one the operator gave, enabling its domains on the new session.
          void this.report("joined", member);
          this.broadcast();
        } else {
          this.scheduleReattach(tab);
        }
      }, REATTACH_MS[attempts]),
    );
  }

  private async attach(member: Member): Promise<boolean> {
    try {
      await chrome.debugger.attach({ tabId: member.tab }, "1.3");
    } catch (error) {
      // A worker restarted under a live session finds the tab still attached to itself.
      if (!/already attached/i.test(errorText(error))) {
        member.attached = false;
        return false;
      }
    }
    member.attached = true;
    member.children = [];
    // The agent's tab is usually in a window that does not have focus. Without focus emulation a
    // page sees `document.hasFocus()` false and blur events, and some sites stop rendering or
    // close their menus the moment the agent opens them.
    try {
      await chrome.debugger.sendCommand({ tabId: member.tab }, "Emulation.setFocusEmulationEnabled", { enabled: true });
    } catch {
      // A page in the middle of navigating can refuse it; the daemon enables it again on its own.
    }
    await this.injectContent(member.tab);
    await this.scope.save();
    return true;
  }

  private async detach(member: Member): Promise<void> {
    clearTimeout(this.reattachTimers.get(member.tab));
    this.reattachTimers.delete(member.tab);
    this.reattachAttempts.delete(member.tab);
    if (!member.attached) return;
    this.markDetached(member, "left");
    try {
      await chrome.debugger.detach({ tabId: member.tab });
    } catch {
      // Already gone with its tab.
    }
  }

  // ---- calls ----

  private async onCall(frame: CallFrame): Promise<void> {
    const args = frame.args ?? {};
    try {
      const value = await this.call(frame.op, args);
      this.send({ t: "result", id: frame.id, ok: true, value });
    } catch (error) {
      const code = error instanceof CallError ? error.code : RESULT_ERRORS.failed;
      this.send({ t: "result", id: frame.id, ok: false, error: { code, message: errorText(error) } });
    }
  }

  private async call(op: string, args: Record<string, unknown>): Promise<unknown> {
    switch (op) {
      case "window.ensure": {
        const group = this.scope.ensureGroup(requireString(args, "group"), optionalString(args, "title"), optionalString(args, "session"));
        const window = await this.ensureWindow(group);
        await this.scope.save();
        // The tabs already in the group come with it, so a daemon that lost track of them (it
        // restarted, or the operator gave tabs before it asked) starts from what is there.
        const tabs = await Promise.all(this.scope.tabsOf(group.id).map((member) => this.describe(member)));
        return { window, tabs };
      }
      case "tab.create":
        return this.createTab(requireString(args, "group"), optionalString(args, "title"), optionalString(args, "session"));
      case "tab.close": {
        const member = this.requireMember(args);
        // Forgotten before it is closed, so its removal, the answer to the daemon's own call, is not
        // reported back as if the operator had closed it.
        await this.detach(member);
        this.scope.remove(member.tab);
        await this.scope.save();
        await chrome.tabs.remove(member.tab).catch(() => undefined);
        await this.refreshScopeViews();
        return {};
      }
      case "tab.activate": {
        // Active within its own window only: focusing the window would pull the operator's keyboard
        // into the agent's window in the middle of whatever they are typing.
        const member = this.requireMember(args);
        await chrome.tabs.update(member.tab, { active: true });
        return {};
      }
      case "window.bounds":
        return this.windowBounds(requireString(args, "group"), args.bounds);
      case "downloads.search": {
        const item = await this.findDownload(requireString(args, "id"));
        return { path: item.filename, state: item.state, bytes: item.bytesReceived };
      }
      case "cookies.names": {
        const url = requireString(args, "url");
        const cookies = await chrome.cookies.getAll({ url });
        // Names and flags, never values: this is how the daemon tells "signed in" from "not", and the
        // value is the one thing that would let anything holding it be the operator.
        return {
          cookies: cookies.map((cookie) => ({
            name: cookie.name,
            domain: cookie.domain,
            path: cookie.path,
            expires: cookie.expirationDate ?? 0,
            secure: cookie.secure,
            http_only: cookie.httpOnly,
            same_site: cookie.sameSite,
            session: cookie.session,
          })),
        };
      }
      case "group.close": {
        const group = this.scope.groups.get(requireString(args, "group"));
        if (group) await this.closeGroup(group);
        return {};
      }
      case "notify":
        await this.showNotice(optionalString(args, "text") ?? "");
        return {};
      case "panel.grant":
        await this.takeGrant({ base: requireString(args, "base"), token: requireString(args, "token"), expires: optionalString(args, "expires") ?? "" });
        return {};
      case "panel.event":
        await this.panelEvent(args);
        return {};
      default:
        throw new CallError(RESULT_ERRORS.failed, `unknown operation ${op}`);
    }
  }

  private requireMember(args: Record<string, unknown>): Member {
    const tab = args.tab;
    const member = typeof tab === "number" ? this.scope.members.get(tab) : undefined;
    if (!member) throw new CallError(RESULT_ERRORS.noTab, "the tab is gone or is not the agent's");
    return member;
  }

  private async ensureWindow(group: Group): Promise<number> {
    if (group.windowId !== undefined) {
      try {
        await chrome.windows.get(group.windowId);
        return group.windowId;
      } catch {
        group.windowId = undefined;
        group.placeholderTab = undefined;
      }
    }
    if (this.settings.windowMode === "current") {
      const window = await chrome.windows.getLastFocused({ windowTypes: ["normal"] });
      group.windowId = window.id!;
      return group.windowId;
    }
    // The agent's own window, same profile and so the same sign-ins, but not focused: a background
    // tab in the operator's window does not paint, and a screenshot of it waits until it times out.
    const window = await chrome.windows.create({ focused: false, type: "normal", state: "normal", url: "about:blank", width: 1280, height: 900 });
    group.windowId = window.id!;
    group.placeholderTab = window.tabs?.[0]?.id;
    return group.windowId;
  }

  private async createTab(groupId: string, title?: string, session?: string): Promise<unknown> {
    const group = this.scope.ensureGroup(groupId, title, session);
    const windowId = await this.ensureWindow(group);
    // Always a blank page. The daemon navigates after it has attached and set up its guard, so that
    // the first page the agent opens is judged like every other; a URL given here would load before
    // anything could judge it.
    const tab = await chrome.tabs.create({ windowId, url: "about:blank", active: this.settings.windowMode === "own" });
    const member = this.scope.add(tab.id!, group.id, false);
    await this.groupTab(group, tab.id!);
    member.grouped = true;
    if (group.placeholderTab !== undefined) {
      const placeholder = group.placeholderTab;
      group.placeholderTab = undefined;
      await chrome.tabs.remove(placeholder).catch(() => undefined);
    }
    await this.attach(member);
    await this.scope.save();
    await this.refreshScopeViews();
    return { tab: { id: tab.id!, group: group.id, url: "about:blank" } satisfies TabInfo };
  }

  /** Puts a tab into the group's Chrome tab group, creating that group in the tab's window if needed. */
  private async groupTab(group: Group, tab: number): Promise<void> {
    if (group.chromeGroupId !== undefined) {
      try {
        await chrome.tabs.group({ tabIds: tab, groupId: group.chromeGroupId });
        return;
      } catch {
        group.chromeGroupId = undefined;
      }
    }
    const current = await chrome.tabs.get(tab);
    group.chromeGroupId = await chrome.tabs.group({ tabIds: tab, createProperties: { windowId: current.windowId } });
    await chrome.tabGroups.update(group.chromeGroupId, {
      title: t("group.prefix", { title: group.title || t("group.untitled") }),
      color: group.color,
    });
  }

  private async windowBounds(groupId: string, bounds: unknown): Promise<unknown> {
    const group = this.scope.groups.get(groupId);
    if (!group || group.windowId === undefined) throw new CallError(RESULT_ERRORS.failed, "the group has no window");
    if (bounds !== undefined) {
      // The operator's own window is theirs to size.
      if (this.settings.windowMode === "current") throw new CallError(RESULT_ERRORS.refused, "the agent works in the operator's window");
      if (typeof bounds !== "object" || bounds === null) throw new CallError(RESULT_ERRORS.failed, "bounds must be an object");
      const b = bounds as Record<string, unknown>;
      const update: chrome.windows.UpdateInfo = {};
      for (const key of ["left", "top", "width", "height"] as const) if (typeof b[key] === "number") update[key] = b[key] as number;
      if (b.state === "normal" || b.state === "minimized" || b.state === "maximized") update.state = b.state;
      await chrome.windows.update(group.windowId, update);
    }
    const window = await chrome.windows.get(group.windowId);
    return { bounds: { left: window.left ?? 0, top: window.top ?? 0, width: window.width ?? 0, height: window.height ?? 0 } };
  }

  /** Forgets the group before closing its tabs, so their removal is not reported as the operator's. */
  private async closeGroup(group: Group): Promise<void> {
    const members = this.scope.dropGroup(group.id);
    for (const member of members) {
      await this.detach(member);
    }
    await this.scope.save();
    const tabs = members.map((member) => member.tab);
    if (group.placeholderTab !== undefined) tabs.push(group.placeholderTab);
    if (tabs.length) await chrome.tabs.remove(tabs).catch(() => undefined);
    await this.refreshScopeViews();
    this.broadcast();
  }

  // ---- the side panel ----

  /**
   * Keeps the side panel's credential and the address the panel frames. On a server that address is
   * the one this Chrome was paired with and already reaches the installation at, whatever the host
   * calls itself (its public address may be a name this laptop does not use); natively it must be this
   * computer. A grant for anywhere else would have the panel frame, and hand the token to, a page
   * nobody chose.
   */
  private async takeGrant(grant: PanelGrant): Promise<void> {
    let origin: string;
    if (this.settings.mode === "server" && this.settings.server) {
      origin = this.settings.server;
    } else {
      let base: URL;
      try {
        base = new URL(grant.base);
      } catch {
        throw new CallError(RESULT_ERRORS.refused, "the panel's address is not a URL");
      }
      const loopback = base.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(base.hostname);
      if (this.settings.mode !== "native" || !loopback) throw new CallError(RESULT_ERRORS.refused, "the panel's address is not this computer");
      origin = base.origin;
    }
    await chrome.storage.session.set({ [GRANT_KEY]: { base: origin, token: grant.token, expires: grant.expires } satisfies PanelGrant });
    void chrome.runtime.sendMessage({ kind: "panel-grant" }).catch(() => undefined);
  }

  async grant(): Promise<PanelGrant | null> {
    const stored = (await chrome.storage.session.get(GRANT_KEY))[GRANT_KEY] as PanelGrant | undefined;
    if (!stored?.token) return null;
    if (stored.expires && Date.parse(stored.expires) < this.now()) return null;
    return stored;
  }

  private async forgetGrant(): Promise<void> {
    await chrome.storage.session.remove(GRANT_KEY);
    void chrome.runtime.sendMessage({ kind: "panel-grant" }).catch(() => undefined);
  }

  /** A conversation using this Chrome needs the operator, or finished: the badge, and a notification
   *  unless the operator is looking at the panel of the window they are in. */
  private async panelEvent(args: Record<string, unknown>): Promise<void> {
    const event: PanelEvent = {
      session: requireString(args, "session"),
      kind: args.kind === "needs_you" ? "needs_you" : "done",
      title: optionalString(args, "title") ?? "",
      waiting: typeof args.waiting === "number" && args.waiting >= 0 ? Math.floor(args.waiting) : 0,
      ...(typeof args.tab === "number" ? { tab: args.tab } : {}),
    };
    this.waiting = event.waiting;
    await chrome.storage.session.set({ panelWaiting: this.waiting });
    this.paintBadge();
    void chrome.runtime.sendMessage({ kind: "panel-event", event }).catch(() => undefined);
    const focused = await chrome.windows.getLastFocused({ windowTypes: ["normal"] }).catch(() => undefined);
    if (focused?.focused && focused.id !== undefined && this.openPanels.has(focused.id)) return;
    // Only a tab of the conversation's own group is named: a click must not focus a tab that the
    // event made up.
    const tab = event.tab !== undefined && this.scope.members.get(event.tab)?.group === this.scope.bySession(event.session)?.id ? event.tab : undefined;
    const id = `${NOTICE_PREFIX}:${event.session}:${tab ?? ""}`;
    await chrome.notifications
      .create(id, {
        type: "basic",
        iconUrl: chrome.runtime.getURL("icon-128.png"),
        title: event.title || t("group.untitled"),
        message: event.kind === "needs_you" ? t("notify.needs_you") : t("notify.done"),
        priority: event.kind === "needs_you" ? 2 : 0,
      })
      .catch(() => undefined);
  }

  /** The conversation that owns a tab, or null when the tab is none of the agent's. */
  tabOwner(tabId: number): TabOwner | null {
    const member = this.scope.members.get(tabId);
    const group = member ? this.scope.groups.get(member.group) : undefined;
    if (!group) return null;
    return { group: group.id, ...(group.session ? { session: group.session } : {}), title: group.title, paused: group.paused };
  }

  /** The agent tabs of a conversation, for the panel's "show the agent's tab". */
  sessionTabs(session: string): number[] {
    const group = this.scope.bySession(session);
    return group ? this.scope.tabsOf(group.id).map((member) => member.tab) : [];
  }

  /**
   * The panel's "let it work on this tab": the operator's tab joins the conversation's group, in the
   * window it is in. The group may not exist yet (a conversation the panel just started): it is made
   * here, in that window, so the daemon's `window.ensure` finds the tab waiting instead of opening a
   * window of its own around nothing.
   */
  async adoptTab(tabId: number, groupId: string, session: string, title: string): Promise<TabInfo> {
    const tab = await chrome.tabs.get(tabId).catch(() => undefined);
    if (!tab || tab.id === undefined) throw new CallError(RESULT_ERRORS.noTab, "the tab is gone");
    if (/^(chrome|chrome-extension|chrome-untrusted|chrome-search|devtools|edge|about):/i.test(tab.url || tab.pendingUrl || "")) {
      throw new CallError(RESULT_ERRORS.refused, "a browser page cannot be given to the agent");
    }
    const held = this.scope.members.get(tabId);
    if (held) {
      if (held.group !== groupId) throw new CallError(RESULT_ERRORS.refused, "the tab already works for another conversation");
      return this.describe(held);
    }
    const group = this.scope.ensureGroup(groupId, title, session);
    if (group.windowId === undefined) group.windowId = tab.windowId;
    await this.groupTab(group, tabId);
    await this.join(tab, group);
    await this.scope.save();
    const member = this.scope.members.get(tabId);
    if (!member) throw new CallError(RESULT_ERRORS.failed, "the tab could not be given");
    return this.describe(member);
  }

  /** The panel's "show the agent's tab" and "take over": that tab, in front, in a focused window. */
  async focusTab(tabId: number): Promise<void> {
    const member = this.scope.members.get(tabId);
    if (!member) throw new CallError(RESULT_ERRORS.noTab, "the tab is not the agent's");
    const tab = await chrome.tabs.update(member.tab, { active: true });
    await chrome.windows.update(tab.windowId, { focused: true });
  }

  // ---- what the operator does to tabs ----

  async onTabCreated(tab: chrome.tabs.Tab): Promise<void> {
    if (tab.id === undefined || tab.openerTabId === undefined || this.scope.members.has(tab.id)) return;
    const opener = this.scope.members.get(tab.openerTabId);
    if (!opener) return;
    // Chrome names an opener for a new tab page opened beside the active tab too, which in the
    // agent's window is an agent tab. That page is the operator's (and Chrome's own pages cannot be
    // debugged anyway), so a browser page never joins by its opener.
    if (/^(chrome|chrome-extension|chrome-untrusted|chrome-search|devtools|edge):/i.test(tab.pendingUrl || tab.url || "")) return;
    const group = this.scope.groups.get(opener.group);
    if (!group) return;
    // A page the agent's tab opened (a popup, a link to a new tab) is the agent's too: the work it
    // was doing continues there, and leaving it out would strand the agent on the opener.
    const member = this.scope.add(tab.id, group.id, false, opener.tab);
    try {
      if (group.chromeGroupId !== undefined && tab.windowId === (await chrome.tabs.get(opener.tab)).windowId) {
        await chrome.tabs.group({ tabIds: tab.id, groupId: group.chromeGroupId });
        member.grouped = true;
      }
    } catch {
      // A popup window cannot hold a tab group; the tab stays a member without one.
    }
    await this.attach(member);
    await this.report("created", member);
    await this.refreshScopeViews();
  }

  async onTabRemoved(tabId: number): Promise<void> {
    clearTimeout(this.reattachTimers.get(tabId));
    this.reattachTimers.delete(tabId);
    this.lastDispatch.delete(tabId);
    this.lastInputReport.delete(tabId);
    for (const group of this.scope.groups.values()) if (group.placeholderTab === tabId) group.placeholderTab = undefined;
    const member = this.scope.remove(tabId);
    if (!member) return;
    if (member.attached) this.markDetached(member, "target_closed");
    await this.scope.save();
    this.send({ t: "tabs", op: "removed", tab: { id: tabId } });
    await this.refreshScopeViews();
  }

  async onTabUpdated(tabId: number, info: chrome.tabs.UpdateInfo, tab: chrome.tabs.Tab): Promise<void> {
    const member = this.scope.members.get(tabId);
    if (info.status === "loading" && member?.attached) await this.injectContent(tabId);
    if (member && (info.url !== undefined || info.title !== undefined)) {
      this.send({
        t: "tabs",
        op: "updated",
        tab: { id: tabId, ...(tab.url ? { url: tab.url } : {}), ...(tab.title ? { title: tab.title } : {}) },
      });
    }
    if (info.groupId === undefined) return;
    if (member) {
      const group = this.scope.groups.get(member.group);
      if (group && info.groupId === group.chromeGroupId) {
        member.grouped = true;
        await this.scope.save();
      } else if (member.grouped) {
        // Dragged out of the group: the operator took the tab back.
        await this.leave(tabId, "left");
      }
      return;
    }
    const group = this.scope.byChromeGroup(info.groupId);
    if (group) await this.join(tab, group);
  }

  /** The operator dragged a tab into an agent group, or gave it through the context menu. */
  private async join(tab: chrome.tabs.Tab, group: Group): Promise<void> {
    if (tab.id === undefined || this.scope.members.has(tab.id)) return;
    const member = this.scope.add(tab.id, group.id, true);
    await this.attach(member);
    await this.report("joined", member);
    await this.refreshScopeViews();
  }

  private async leave(tabId: number, op: "left"): Promise<void> {
    const member = this.scope.members.get(tabId);
    if (!member) return;
    await this.detach(member);
    await this.border(tabId, null);
    this.scope.remove(tabId);
    await this.scope.save();
    this.send({ t: "tabs", op, tab: { id: tabId } });
    await this.refreshScopeViews();
  }

  async onGroupRemoved(chromeGroup: chrome.tabGroups.TabGroup): Promise<void> {
    const group = this.scope.byChromeGroup(chromeGroup.id);
    if (!group) return;
    // Closing the group is the operator ending the agent's work there. Tabs still held (a popup in
    // its own window) are let go rather than closed: they are on the operator's screen now.
    for (const member of this.scope.dropGroup(group.id)) {
      await this.detach(member);
      await this.border(member.tab, null);
    }
    await this.scope.save();
    this.send({ t: "tabs", op: "group_closed", group: group.id });
    await this.refreshScopeViews();
  }

  async onMenuClicked(info: chrome.contextMenus.OnClickData, tab?: chrome.tabs.Tab): Promise<void> {
    const id = String(info.menuItemId);
    if (!id.startsWith(`${MENU_ROOT}:`) || tab?.id === undefined) return;
    const group = this.scope.groups.get(id.slice(MENU_ROOT.length + 1));
    if (!group || this.scope.members.has(tab.id)) return;
    await this.groupTab(group, tab.id);
    // `tabs.onUpdated` reports the new group too, but not necessarily after the line above has
    // recorded the group's id; joining here directly makes the order not matter.
    await this.join(tab, group);
  }

  /** A content script saw the operator's own trusted input in an agent tab. */
  onContentInput(sender: chrome.runtime.MessageSender): void {
    const tabId = sender.tab?.id;
    if (tabId === undefined || !this.scope.members.has(tabId)) return;
    const now = this.now();
    if (now - (this.lastDispatch.get(tabId) ?? -Infinity) < OWN_INPUT_MS) return;
    if (now - (this.lastInputReport.get(tabId) ?? -Infinity) < INPUT_REPORT_MS) return;
    this.lastInputReport.set(tabId, now);
    this.send({ t: "input", tab: tabId });
  }

  // ---- what the operator sees ----

  private async report(op: "created" | "joined", member: Member): Promise<void> {
    this.send({ t: "tabs", op, tab: await this.describe(member) });
  }

  /** A member as the daemon names a tab, with empty fields left out as the daemon writes them. */
  private async describe(member: Member): Promise<TabInfo> {
    const tab = await chrome.tabs.get(member.tab).catch(() => undefined);
    const url = tab?.url || tab?.pendingUrl;
    return {
      id: member.tab,
      group: member.group,
      ...(member.opener === undefined ? {} : { opener: member.opener }),
      ...(url ? { url } : {}),
      ...(tab?.title ? { title: tab.title } : {}),
    };
  }

  /**
   * The chrome.downloads item behind a DevTools download guid: the newest download of the address
   * Page.downloadWillBegin named, begun no earlier than a little before that event. Only downloads an
   * agent tab began are found, so the operator's download history stays out of reach.
   */
  private async findDownload(guid: string): Promise<chrome.downloads.DownloadItem> {
    const seen = this.downloads.get(guid);
    if (!seen) throw new CallError(RESULT_ERRORS.failed, "no download of an agent tab has this id");
    const items = await chrome.downloads.search({
      startedAfter: new Date(seen.at - 10_000).toISOString(),
      orderBy: ["-startTime"],
      limit: 50,
    });
    const item = items.find((candidate) => candidate.url === seen.url || candidate.finalUrl === seen.url);
    if (!item) throw new CallError(RESULT_ERRORS.failed, "the download has not reached Chrome's list yet");
    return item;
  }

  private async showNotice(text: string): Promise<void> {
    this.notice = { at: this.now(), text };
    await chrome.storage.session.set({ notice: this.notice });
    this.flashUntil = this.now() + FLASH_MS;
    clearTimeout(this.flashTimer);
    this.flashTimer = setTimeout(() => this.paintBadge(), FLASH_MS);
    this.paintBadge();
    void chrome.runtime.sendMessage({ kind: "notice", notice: this.notice }).catch(() => undefined);
  }

  private setPhase(phase: Phase): void {
    this.phase = phase;
    if (phase !== "error") {
      this.error = undefined;
      this.errorName = undefined;
    }
    this.broadcast();
  }

  private broadcast(): void {
    this.paintBadge();
    void chrome.runtime.sendMessage({ kind: "status" }).catch(() => undefined);
  }

  private paintBadge(): void {
    let color = COLOURS.idle;
    let text = "";
    if (this.now() < this.flashUntil) {
      color = COLOURS.flash;
      text = "✓";
    } else if (this.phase === "connected" && this.waiting > 0) {
      // A conversation waiting for the operator matters more than how many tabs the agent holds.
      color = COLOURS.waiting;
      text = String(this.waiting);
    } else if (this.phase === "connected") {
      color = COLOURS.connected;
      text = this.scope.members.size ? String(this.scope.members.size) : "";
    } else if (this.phase === "error") {
      color = COLOURS.error;
      text = "!";
    } else if (this.phase === "connecting") {
      text = "…";
    }
    const state = this.error ? t(`error.${this.error}` as Key, { name: this.errorName ?? "" }) : t(`phase.${this.phase}` as Key);
    void chrome.action.setBadgeBackgroundColor({ color }).catch(() => undefined);
    void chrome.action.setBadgeText({ text }).catch(() => undefined);
    void chrome.action.setTitle({ title: t("badge.title", { state }) }).catch(() => undefined);
  }

  /** Rebuilds what follows from scope: the badge, the request guard and the context menu. */
  private async refreshScopeViews(): Promise<void> {
    this.broadcast();
    await updateGuard([...this.scope.members.keys()]);
    await chrome.contextMenus.removeAll().catch(() => undefined);
    const groups = [...this.scope.groups.values()];
    chrome.contextMenus.create({ id: MENU_ASK, title: t("menu.ask"), contexts: ["page", "selection"] });
    chrome.contextMenus.create({
      id: MENU_ROOT,
      title: groups.length ? t("menu.grant") : t("menu.grant_none"),
      contexts: ["page"],
      enabled: groups.length > 0,
    });
    for (const group of groups) {
      chrome.contextMenus.create({
        id: `${MENU_ROOT}:${group.id}`,
        parentId: MENU_ROOT,
        title: t("menu.grant_to", { title: group.title || t("group.untitled") }),
        contexts: ["page"],
      });
    }
  }

  private async injectContent(tabId: number): Promise<void> {
    try {
      await chrome.scripting.executeScript({ target: { tabId }, files: ["content.js"], injectImmediately: true });
    } catch {
      // Pages Chrome keeps extensions out of (the Web Store, other extensions) get no border.
    }
  }

  /** Moves the agent's cursor in the tab to where the daemon acts. A child session (a frame of another
   *  site) is left out: its points are in that frame, not in the page the cursor is drawn over. */
  private async cursor(tabId: number, method: string, params: Record<string, unknown>): Promise<void> {
    const at = cursorPoint(method, params);
    if (!at) return;
    await chrome.tabs.sendMessage(tabId, { kind: "daedalus-cursor", ...at }).catch(() => undefined);
  }

  /** Shows, hides (for a screenshot) or removes (null, the tab left) the border on an agent tab. */
  private async border(tabId: number, visible: boolean | null): Promise<void> {
    // Hiding waits for the page to say it painted, never longer than BORDER_HIDE_MS: a page that does
    // not answer must not hold the daemon's screenshot.
    const told = chrome.tabs.sendMessage(tabId, { kind: "daedalus-border", visible }).catch(() => undefined);
    await (visible === false ? Promise.race([told, new Promise((done) => setTimeout(done, BORDER_HIDE_MS))]) : told);
  }
}

/** Where an `Input.*` command acts, for the cursor: a point and whether it presses, or typing at the
 *  last point. A move without a button is followed too, so the cursor glides to what is clicked next;
 *  a release says nothing new. */
export function cursorPoint(method: string, params: Record<string, unknown>): { x?: number; y?: number; click: boolean; typing: boolean } | null {
  const num = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : NaN);
  if (method === "Input.dispatchMouseEvent" || method === "Input.dispatchDragEvent") {
    if (params.type === "mouseReleased") return null;
    const x = num(params.x);
    const y = num(params.y);
    if (Number.isNaN(x) || Number.isNaN(y)) return null;
    return { x, y, click: params.type === "mousePressed", typing: false };
  }
  if (method === "Input.dispatchTouchEvent" && params.type === "touchStart") {
    const first = Array.isArray(params.touchPoints) ? (params.touchPoints[0] as Record<string, unknown> | undefined) : undefined;
    const x = num(first?.x);
    const y = num(first?.y);
    return Number.isNaN(x) || Number.isNaN(y) ? null : { x, y, click: true, typing: false };
  }
  // No point of its own: a NaN would cross the message as null and read as the corner.
  if (method === "Input.dispatchKeyEvent" || method === "Input.insertText") return { click: false, typing: true };
  return null;
}

/** The longest a screenshot waits for the agent's marks to leave the picture. */
export const BORDER_HIDE_MS = 400;

class CallError extends Error {
  constructor(readonly code: string, message: string) {
    super(message);
  }
}

function requireString(args: Record<string, unknown>, key: string): string {
  const value = args[key];
  if (typeof value !== "string" || !value) throw new CallError(RESULT_ERRORS.failed, `${key} must be a string`);
  return value;
}

function optionalString(args: Record<string, unknown>, key: string): string | undefined {
  const value = args[key];
  return typeof value === "string" ? value : undefined;
}

function errorText(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** Chrome's version from the user agent; the reduced user agent still carries the major version. */
export function chromeVersion(): string {
  const match = /Chrome\/([\d.]+)/.exec(navigator.userAgent);
  return match ? match[1] : "unknown";
}
