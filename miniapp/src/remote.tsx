// Remote chats and the computers that run them (docs/architecture/cli.md).
//
// A remote chat is a conversation started by daedalus-cli in a folder on one of the operator's
// computers. The loop, the memory and the transcript stay on the server like any other chat's; only
// the shell and file tools run on that computer, through the link the CLI holds open. So the app
// shows the chat as it shows every chat, readable, answerable and approvable from here, and adds
// three things: which computer and folder it works in, whether that computer is connected, and —
// when the computer dropped while a tool waited on it — that the turn is paused rather than failed.
// The turn goes on by itself when the CLI reconnects; Cancel turn (the run's Stop) is the way out
// when it will not.
//
// Pairing works like a device code. `daedalus-cli login` shows a code and asks the server; the app
// lists the request and the operator approves it here, inside a signed-in app session, so the
// two-factor gate in front of the app covers the pairing too. The request is announced by a small
// notice and the sheet opens only from it, from the link the CLI prints (`?pair-device=<code>`), or
// from the code typed under Settings → Devices: anyone who can reach the server can ask, and a sheet
// that opened by itself would put an Approve button under a pointer that was on its way elsewhere.

import { useEffect, useReducer, useState, useSyncExternalStore } from "react";
import { ApiError, api, type PairRequest, type RemoteBinding, type SessionSummary } from "./api";
import { useEvent } from "./events";
import { absDate, relTimeLong } from "./format";
import { t } from "./i18n";
import { Icon } from "./icons";
import { invalidate, useQuery } from "./store";
import { errorText } from "./ui";
import { Sheet, toast } from "./ui/dialogs";

export const CLI_DEVICES = "/api/cli/devices";
export const PAIRS = "/api/cli/pairs";

/** The terminal a chat is bound to, for a chat started from one; nothing for every other chat. */
export function remoteOf(s: Pick<SessionSummary, "kind" | "remote"> | null | undefined): RemoteBinding | null {
  return s?.kind === "remote" && s.remote ? s.remote : null;
}

/** What the header says about the link: the turn waiting for the computer outranks the link itself,
 *  because it is the one state that asks something of the operator. */
export type RemoteState = "connected" | "reconnecting" | "offline" | "paused";

/** How long after its link drops a device reads as reconnecting rather than offline: the client
 *  retries on its own within seconds, so a drop that lasts longer than this is somebody's laptop
 *  lid, not a hiccup. */
export const RECONNECT_GRACE_MS = 45_000;

// When each device's link was last seen to drop, by this window. The server says only whether a link
// is up; the moment it went down arrives once, as `device.connection`, and is kept here so the header
// can tell a client that is retrying from one that is gone. A window opened after the drop never saw
// it, and says offline, which is what such a device most likely is.
const droppedAt = new Map<string, number>();
const linkListeners = new Set<() => void>();

/** Note a device's link going up or down, as the global stream reports it. */
export function noteConnection(deviceId: string, connected: boolean, now = Date.now()): void {
  if (connected) droppedAt.delete(deviceId);
  else droppedAt.set(deviceId, now);
  for (const listener of [...linkListeners]) listener();
}

export function remoteState(b: RemoteBinding, now = Date.now()): RemoteState {
  if (b.waiting) return "paused";
  if (b.connected) return "connected";
  const dropped = droppedAt.get(b.device_id);
  return dropped !== undefined && now - dropped < RECONNECT_GRACE_MS ? "reconnecting" : "offline";
}

/** The state of a chat's link, kept current: the stream's drops and returns redraw it, and a
 *  reconnecting device turns offline by itself once the grace is over. The drops themselves are
 *  noted by PairingHost, which every signed-in window mounts. */
export function useRemoteState(b: RemoteBinding | null): RemoteState | null {
  const [, redraw] = useReducer((n: number) => n + 1, 0);
  useEffect(() => {
    linkListeners.add(redraw);
    return () => {
      linkListeners.delete(redraw);
    };
  }, []);
  const state = b ? remoteState(b) : null;
  const dropped = b ? droppedAt.get(b.device_id) : undefined;
  useEffect(() => {
    if (state !== "reconnecting" || dropped === undefined) return;
    const timer = window.setTimeout(redraw, Math.max(0, dropped + RECONNECT_GRACE_MS - Date.now()) + 50);
    return () => window.clearTimeout(timer);
  }, [state, dropped]);
  return state;
}

/**
 * A working folder short enough for a header: the home directory as `~`, and only the last two
 * folders of a deep path. The whole path stays in the tooltip. The home is guessed from the path
 * itself, because the server never learns the computer's home, and the three shapes cover macOS,
 * Linux and Windows.
 */
export function shortFolder(cwd: string): string {
  const path = cwd.replace(/^\/(?:home|Users)\/[^/]+(?=\/|$)/, "~").replace(/^[A-Za-z]:\\Users\\[^\\]+(?=\\|$)/, "~");
  const sep = path.includes("\\") && !path.includes("/") ? "\\" : "/";
  const parts = path.split(sep).filter(Boolean);
  if (parts.length <= 3) return path || sep;
  return ["…", ...parts.slice(-2)].join(sep);
}

/** The device and the folder, as one line: what the phone's header says. */
export function remotePlace(b: RemoteBinding): string {
  return `${b.device_name} · ${shortFolder(b.cwd)}`;
}

/** The operating system as people name it, with the processor when it says something. */
export function osLabel(os: string, arch = ""): string {
  const name = ({ linux: "Linux", darwin: "macOS", macos: "macOS", windows: "Windows", freebsd: "FreeBSD" } as Record<string, string>)[os.toLowerCase()] ?? os;
  const cpu = ({ amd64: "x86-64", x86_64: "x86-64", arm64: "ARM", aarch64: "ARM" } as Record<string, string>)[arch.toLowerCase()] ?? arch;
  return [name, cpu].filter(Boolean).join(" · ");
}

/**
 * The remote line of a chat's header: the computer, the folder, the state of the link, and the
 * terminal's guard when it is not the default. A paused turn reads as a state too, in the warning
 * colour; the banner under the header says what to do about it.
 */
export function RemoteHead({ b, state }: { b: RemoteBinding; state: RemoteState }) {
  const guard = b.guard === "ask" || b.guard === "plan" ? b.guard : null;
  return (
    <span className="remote-head" data-remote-state={state} title={`${b.device_name} · ${b.cwd}`}>
      <Icon name="laptop" size={13} />
      <span className="truncate remote-device">{b.device_name}</span>
      <span className="sep">·</span>
      <span className="truncate remote-folder">{shortFolder(b.cwd)}</span>
      <span className="remote-state" role="status"><span className="remote-dot" aria-hidden />{t(`remote.state.${state}`)}</span>
      {guard && <span className="remote-guard" data-guard={guard} title={t(`remote.guard.${guard}.title`)}>{t(`remote.guard.${guard}`)}</span>}
    </span>
  );
}

/**
 * The paused turn: the computer the turn's tools run on went away mid-turn, and the server holds the
 * turn until it comes back rather than failing the tool. Said once, under the header, with the one
 * thing the operator can do about it. Reading, answering and approving go on as usual meanwhile.
 */
export function RemotePaused({ b, onCancel }: { b: RemoteBinding; onCancel: () => Promise<unknown> | void }) {
  const [busy, setBusy] = useState(false);
  if (!b.waiting) return null;
  const since = typeof b.waiting === "object" ? b.waiting.since : "";
  return (
    <div className="remote-paused" role="status" data-remote-paused="">
      <span className="remote-paused-icon"><Icon name="pause" size={16} /></span>
      <span className="remote-paused-text">
        <b>{t("remote.paused.title", { device: b.device_name })}</b>
        <span>{since ? `${t("remote.paused.since", { t: relTimeLong(since) })} ` : ""}{t("remote.paused.sub")}</span>
      </span>
      <button type="button" className="btn small" disabled={busy} data-action="cancel-turn"
        onClick={() => { setBusy(true); void Promise.resolve(onCancel()).finally(() => setBusy(false)); }}>{t("remote.paused.cancel")}</button>
    </div>
  );
}

/** A remote chat's computer, for a row's meta line: its name, and "paused" while a turn waits for it. */
export function remoteRowWords(s: Pick<SessionSummary, "kind" | "remote">): string {
  const b = remoteOf(s);
  if (!b) return "";
  return b.waiting ? `${b.device_name} · ${t("remote.state.paused.short")}` : b.device_name;
}

// The pairing sheet is one for the whole app: the notice, the link and the Settings field all open
// it, and two of them at once must not stack two sheets.
let pairing: string | null = null;
const pairingListeners = new Set<() => void>();

/** Open the approval sheet for a code. */
export function openPairing(code: string): void {
  pairing = code;
  for (const listener of [...pairingListeners]) listener();
}

function closePairing(): void {
  pairing = null;
  for (const listener of [...pairingListeners]) listener();
}

function subscribePairing(listener: () => void): () => void {
  pairingListeners.add(listener);
  return () => {
    pairingListeners.delete(listener);
  };
}

/** A code as typed or printed — any case, spaces or a dash between its halves — reduced to compare. */
export function normCode(code: string): string {
  return code.toUpperCase().replace(/[^A-Z0-9]/g, "");
}

/** The code the way the terminal prints it: two halves with a dash, easier to read aloud. */
export function showCode(code: string): string {
  const plain = normCode(code);
  return plain.length === 8 ? `${plain.slice(0, 4)}-${plain.slice(4)}` : plain;
}

/** The request a code names among those waiting, by its normalised form. */
export function findRequest(requests: PairRequest[], code: string): PairRequest | null {
  const want = normCode(code);
  return requests.find((r) => normCode(r.user_code) === want) ?? null;
}

/** The address bar's `?pair-device=` taken once and removed, so a reload does not ask again. */
function takePairLink(): string | null {
  try {
    const url = new URL(window.location.href);
    const code = url.searchParams.get("pair-device");
    if (!code) return null;
    url.searchParams.delete("pair-device");
    window.history.replaceState(window.history.state, "", url.pathname + url.search + url.hash);
    return code;
  } catch {
    return null;
  }
}

/** The requests still waiting for an answer: anything decided or out of time is not one any more. */
function useRequests(): PairRequest[] {
  const read = useQuery<{ requests: PairRequest[] }>(PAIRS, { staleMs: 15000 });
  useEvent(["device.pairing"], () => invalidate(PAIRS));
  const now = Date.now();
  return (read.data?.requests ?? []).filter((r) => r.state === "pending" && (!r.expires_at || Date.parse(r.expires_at) > now));
}

/** One request by its code, for a sheet opened from a link or a typed code: the list in hand may be
 *  older than a code printed a moment ago, so the server is asked about that code itself. */
function useRequest(code: string | null, listed: PairRequest | null): { request: PairRequest | null; missing: boolean } {
  const [read, setRead] = useState<{ code: string; request: PairRequest | null } | null>(null);
  useEffect(() => {
    setRead(null);
    if (!code || listed) return;
    let gone = false;
    api.get<PairRequest>(`/api/cli/pair/${encodeURIComponent(showCode(code))}`)
      .then((request) => { if (!gone) setRead({ code, request: request?.state === "pending" ? request : null }); })
      .catch(() => { if (!gone) setRead({ code, request: null }); });
    return () => {
      gone = true;
    };
  }, [code, listed]);
  if (listed) return { request: listed, missing: false };
  const answered = read && read.code === code ? read : null;
  return { request: answered?.request ?? null, missing: !!answered && !answered.request };
}

/**
 * Everything pairing draws outside Settings: the notice about a terminal waiting, and the approval
 * sheet. Mounted once beside the other hosts of the app.
 */
export function PairingHost() {
  const open = useSyncExternalStore(subscribePairing, () => pairing, () => pairing);
  const requests = useRequests();
  // Drops are noted here, in the host every signed-in window mounts, so a chat opened a few seconds
  // after its computer dropped still says it reconnects.
  useEvent(["device.connection"], (event) => {
    const id = String(event.payload?.device_id ?? "");
    if (id) noteConnection(id, !!event.payload?.connected);
  });
  const [dismissed, setDismissed] = useState<Set<string>>(() => new Set());
  useEffect(() => {
    const code = takePairLink();
    if (code) openPairing(code);
  }, []);
  const { request, missing } = useRequest(open, open ? findRequest(requests, open) : null);
  // The list is newest first; the notice names the newest one, and the rest wait behind it.
  const newest = requests.find((r) => !dismissed.has(normCode(r.user_code)));
  return (
    <>
      {newest && !open && (
        <div className="pair-notice" role="alert" data-pair-code={newest.user_code}>
          <span className="pair-notice-icon"><Icon name="laptop" size={18} /></span>
          <span className="pair-notice-text">
            <b>{t("pair.notice.title")}</b>
            <span className="truncate">{[newest.name, osLabel(newest.os)].filter(Boolean).join(" · ")}</span>
          </span>
          <button type="button" className="btn small primary" onClick={() => openPairing(newest.user_code)}>{t("pair.notice.review")}</button>
          <button type="button" className="iconbtn small quiet" aria-label={t("pair.notice.later")} title={t("pair.notice.later")}
            onClick={() => setDismissed((d) => new Set(d).add(normCode(newest.user_code)))}><Icon name="close" size={14} /></button>
        </div>
      )}
      {open && <PairSheet code={open} request={request} loaded={!!request || missing} onClose={closePairing} />}
    </>
  );
}

/** How long a request has left, as m:ss; "" once it is out of time. */
export function timeLeft(expires: string, now = Date.now()): string {
  const left = Math.floor((Date.parse(expires) - now) / 1000);
  if (!Number.isFinite(left) || left <= 0) return "";
  return `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
}

function useSecond(on: boolean): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    if (!on) return;
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, [on]);
  return now;
}

/**
 * The approval: which computer asks, from which system, the code it shows, and when it asked. The
 * code is the check — the operator compares it with the one in their terminal — so it is the largest
 * thing on the sheet. A code nobody is waiting with gets a sentence instead of buttons, and a request
 * answered meanwhile elsewhere (Telegram's Allow, another window) says so rather than failing.
 */
export function PairSheet({ code, request, loaded, onClose }: { code: string; request: PairRequest | null; loaded: boolean; onClose: () => void }) {
  const [busy, setBusy] = useState<"approve" | "deny" | null>(null);
  const [error, setError] = useState("");
  const now = useSecond(!!request);
  const left = request?.expires_at ? timeLeft(request.expires_at, now) : "";
  async function act(how: "approve" | "deny") {
    if (!request) return;
    setBusy(how);
    setError("");
    try {
      await api.post(`/api/cli/pair/${encodeURIComponent(request.user_code)}/${how}`, how === "approve" ? {} : undefined);
      invalidate(PAIRS);
      invalidate(CLI_DEVICES);
      toast(t(how === "approve" ? "pair.approved" : "pair.denied", { name: request.name }));
      onClose();
    } catch (e) {
      setError(e instanceof ApiError && e.status === 409 ? t("pair.decided") : e instanceof ApiError && e.status === 404 ? t("pair.missing") : errorText(e));
      invalidate(PAIRS);
    } finally {
      setBusy(null);
    }
  }
  return (
    <Sheet title={t("pair.sheet.title")} onClose={onClose} size="narrow" className="pair-sheet">
      {!request ? (
        <div className="pair-missing" data-pair-state={loaded ? "missing" : "loading"}>
          {loaded ? <>
            <div className="pair-code" aria-label={t("pair.sheet.code")}>{showCode(code)}</div>
            <p>{t("pair.missing")}</p>
          </> : <p className="sub">{t("common.loading")}</p>}
        </div>
      ) : (
        <div className="pair-body" data-pair-state={left ? "open" : "expired"}>
          <div className="pair-device">
            <span className="pair-device-icon"><Icon name="laptop" size={22} /></span>
            <span className="pair-device-text">
              <b className="truncate" data-pair-name>{request.name}</b>
              <span className="sub">{[osLabel(request.os, request.arch), t("pair.sheet.asked", { t: relTimeLong(request.created_at) })].filter(Boolean).join(" · ")}</span>
            </span>
          </div>
          <div className="pair-code-label sub">{t("pair.sheet.code")}</div>
          <div className="pair-code" data-pair-code>{showCode(request.user_code)}</div>
          <p className="pair-check">{t("pair.sheet.check")}</p>
          <p className="sub pair-grants">{t("pair.sheet.grants")}</p>
          <div className="sub pair-when" title={[absDate(request.created_at), request.address, request.client_version && `daedalus-cli ${request.client_version}`].filter(Boolean).join(" · ")}>
            {left ? t("pair.sheet.left", { t: left }) : t("pair.sheet.expired")}
          </div>
          {error && <div className="pair-error" role="alert">{error}</div>}
          <div className="pair-actions">
            <button type="button" className="btn" disabled={!!busy} data-action="deny" onClick={() => void act("deny")}>{t("pair.deny")}</button>
            <button type="button" className="btn primary" disabled={!!busy || !left} data-action="approve" onClick={() => void act("approve")}>{t("pair.approve")}</button>
          </div>
        </div>
      )}
    </Sheet>
  );
}
