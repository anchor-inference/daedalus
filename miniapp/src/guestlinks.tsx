// The agent link: the sheet a chat opens to hand someone a link, and the card a guest's message is drawn
// as in the chat. A guest is a stranger, so everything they wrote is shown as plain text, in a frame
// that says whose it is, and nothing here turns it into markdown, a link or a picture.

import { useEffect, useRef, useState } from "react";
import { api, GuestDefaults, GuestDoor, GuestExchange, GuestInvite, GuestLink, GuestLinkBody, GuestLinkDetail, GuestMessage, GuestBilling, GuestMode, GuestTools, LinkFile, LinkTask } from "./api";
import { absTime, relTimeLong, usd } from "./format";
import { Icon } from "./icons";
import { plural, t } from "./i18n";
import { GuestHeldApproval, kindWord, LinkFiles, TaskList } from "./linkfiles";
import { AddressNote } from "./publicaddress";
import { invalidate, useQuery } from "./store";
import { Dropdown, Segmented, Switch, useCopied } from "./ui/components";
import { Sheet } from "./ui/dialogs";
import { confirmAsync, errorText } from "./ui";
import "./guestlinks.css";

const LINKS = "/api/guest/links";
const POLL_MS = 5000;

/** Epoch seconds, as the door writes them, to the milliseconds the formatters read. */
const ms = (seconds: number) => seconds * 1000;

/** What the Copy button puts on the clipboard: one line of intent, then the address. A bare address
 *  does not start a conversation: the guest's agent has to be told what the address is for. The intro
 *  stays when there is a topic: with the topic alone, four of five real runs took the address for the
 *  service being asked about instead of a way to talk to anyone. */
export function inviteText(topic: string, url: string): string {
  const said = topic.trim().replace(/[\s:.,;!?]+$/, "");
  return `${t("guest.intro")}${said ? `. ${said}` : ""}: ${url}`;
}

/** The word and tone a link is shown with: its own state until it is spent, then its conversation's. */
export function linkState(link: Pick<GuestLink, "state" | "conversation">): { key: string; tone: "" | "attn" | "ok" } {
  if (link.state === "revoked" || link.state === "expired") return { key: link.state, tone: "" };
  if (link.state === "waiting") return { key: "waiting", tone: "" };
  const conversation = link.conversation;
  if (!conversation) return { key: "used", tone: "" };
  return { key: conversation.state, tone: conversation.state === "pending" ? "attn" : conversation.state === "open" ? "ok" : "" };
}

const encoded = encodeURIComponent;

type View = { kind: "list" } | { kind: "form" } | { kind: "edit"; id: string } | { kind: "made"; url: string; topic: string } | { kind: "read"; id: string };

export function GuestLinksSheet({ sessionId, toast, onClose }: { sessionId: string; toast: (text: string) => void; onClose: () => void }) {
  const [view, setView] = useState<View>({ kind: "list" });
  // Asked about this chat: whether it can be given the sandboxed tools is a fact about the chat.
  const door = useQuery<GuestDoor>(`/api/guest/door?session_id=${encoded(sessionId)}`, { pollMs: 15000 });
  const data = door.data;
  return (
    <Sheet title={t("guest.title")} onClose={onClose} size="narrow">
      <div className="guest-sheet" data-guest-sheet="">
        {!data ? (
          <div className="sub" role="status">{door.error ? t("guest.off") : t("common.loading")}</div>
        ) : !data.available ? (
          <div className="sub" data-guest-off="off">{t("guest.off")}</div>
        ) : !data.public_base ? (
          <>
            <div className="sub" data-guest-off="address">{t("guest.noaddress")}</div>
            <AddressNote />
          </>
        ) : view.kind === "form" ? (
          <GuestForm sessionId={sessionId} door={data} onCancel={() => setView({ kind: "list" })} onMade={(url, topic) => setView({ kind: "made", url, topic })} onSaved={() => setView({ kind: "list" })} />
        ) : view.kind === "edit" ? (
          <GuestEditor id={view.id} sessionId={sessionId} door={data} onBack={() => setView({ kind: "list" })} onSaved={() => setView({ kind: "list" })} />
        ) : view.kind === "made" ? (
          <MadeLink url={view.url} topic={view.topic} toast={toast} onDone={() => setView({ kind: "list" })} />
        ) : view.kind === "read" ? (
          <GuestReader id={view.id} sessionId={sessionId} toast={toast} onBack={() => setView({ kind: "list" })} />
        ) : (
          <GuestList sessionId={sessionId} door={data} toast={toast} onNew={() => setView({ kind: "form" })} onRead={(id) => setView({ kind: "read", id })} onEdit={(id) => setView({ kind: "edit", id })} onMade={(url, topic) => setView({ kind: "made", url, topic })} />
        )}
      </div>
    </Sheet>
  );
}

// ── the list ─────────────────────────────────────────────────────────────────────────────

function GuestList({ sessionId, door, toast, onNew, onRead, onEdit, onMade }: {
  sessionId: string;
  door: GuestDoor;
  toast: (text: string) => void;
  onNew: () => void;
  onRead: (id: string) => void;
  onEdit: (id: string) => void;
  onMade: (url: string, topic: string) => void;
}) {
  const links = useQuery<{ links: GuestLink[] }>(`${LINKS}?session_id=${encoded(sessionId)}`, { pollMs: POLL_MS });
  const [busy, setBusy] = useState<string | null>(null);

  async function act(key: string, work: () => Promise<void>) {
    if (busy) return;
    setBusy(key);
    try {
      await work();
      invalidate("/api/guest/");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  const decide = (link: GuestLink, decision: "admit" | "refuse") => act(`${decision}:${link.id}`, async () => {
    await api.post(`/api/guest/conversations/${encoded(link.conversation!.id)}/${decision}`);
  });
  const revoke = (link: GuestLink) => act(`revoke:${link.id}`, async () => {
    await api.post(`${LINKS}/${encoded(link.id)}/revoke`);
  });
  const reissue = (link: GuestLink) => act(`reissue:${link.id}`, async () => {
    const made = await api.post<GuestInvite>(`${LINKS}/${encoded(link.id)}/reissue`);
    onMade(made.url, made.link.topic);
  });

  // What waits for an answer first, then what is live, then what is over; the newest first within each.
  const rank = (link: GuestLink) => (link.conversation?.state === "pending" ? 0 : link.state === "revoked" || link.state === "expired" ? 2 : 1);
  const rows = [...(links.data?.links ?? [])].sort((a, b) => rank(a) - rank(b) || b.created_at - a.created_at);
  return (
    <>
      <div className="guest-bar">
        <button type="button" className="btn primary" data-guest-new="" onClick={onNew}><Icon name="plus" size={14} />{t("guest.make")}</button>
      </div>
      {!links.data ? (
        <div className="sub" role="status">{links.error ?? t("common.loading")}</div>
      ) : rows.length === 0 ? (
        <div className="sub">{t("guest.empty")}</div>
      ) : (
        <div className="guest-list">
          {rows.map((link) => (
            <GuestLinkCard key={link.id} link={link} busy={busy !== null} onDecide={decide} onRevoke={revoke} onReissue={reissue} onRead={onRead} onEdit={onEdit} />
          ))}
        </div>
      )}
      <DailyLimit door={door} toast={toast} />
    </>
  );
}

function GuestLinkCard({ link, busy, onDecide, onRevoke, onReissue, onRead, onEdit }: {
  link: GuestLink;
  busy: boolean;
  onDecide: (link: GuestLink, decision: "admit" | "refuse") => void;
  onRevoke: (link: GuestLink) => void;
  onReissue: (link: GuestLink) => void;
  onRead: (id: string) => void;
  onEdit: (id: string) => void;
}) {
  const state = linkState(link);
  const conversation = link.conversation;
  const pending = conversation?.state === "pending";
  const live = link.state === "waiting" || link.state === "spent";
  const level = levelOf(link);
  const meta = [
    t(`guest.level.${level}`),
    level === "stand_in" ? link.model_name : level === "direct" ? t(`guest.tools.${link.guest_tools}`) : "",
    // A limit of 0 is none: the sum spent alone, since "of $0" would read as a link that may spend nothing.
    link.priced ? (link.usd_limit ? `${usd(link.spent_usd)} / ${usd(link.usd_limit)}` : t("guest.spent.nolimit", { sum: usd(link.spent_usd) })) : "",
    plural("guest.turns", link.turns_used),
    link.state === "waiting" ? (link.invite_expires_at ? t("guest.expires", { when: relTimeLong(ms(link.invite_expires_at)) }) : t("guest.expires.never")) : "",
    link.conversation?.client === "daedalus" ? t("guest.client.daedalus") : "",
  ].filter(Boolean).join(" · ");
  return (
    <div className="guest-link" data-guest-link={link.id} data-state={state.key}>
      <div className="guest-link-head">
        <span className="guest-name truncate">{link.label || link.topic || t("guest.untitled")}</span>
        <span className={`chip ${state.tone}`} data-guest-state="">{t(`guest.state.${state.key}`)}</span>
      </div>
      <div className="sub guest-link-meta">{meta}</div>
      {pending && conversation && (
        <>
          <blockquote className="guest-quote" data-guest-quote="">
            <div className="sub">{t("guest.first")}</div>
            <div className="guest-quote-text">{conversation.first_message ?? ""}</div>
          </blockquote>
          <div className="sub">{t("guest.decide", { time: absTime(ms(conversation.decide_by)) })}</div>
        </>
      )}
      {link.refused_redeems > 0 && link.state !== "revoked" && (
        <div className="sub attn" data-guest-redeems={link.refused_redeems}>{plural("guest.redeems", link.refused_redeems)}</div>
      )}
      <div className="guest-actions">
        {pending && <button type="button" className="btn small primary" disabled={busy} onClick={() => onDecide(link, "admit")}>{t("guest.admit")}</button>}
        {pending && <button type="button" className="btn small" disabled={busy} onClick={() => onDecide(link, "refuse")}>{t("guest.refuse")}</button>}
        {conversation && !pending && <button type="button" className="btn small" onClick={() => onRead(link.id)}>{t("guest.read")}</button>}
        {link.refused_redeems > 0 && link.state !== "revoked" && <button type="button" className="btn small" disabled={busy} title={t("guest.reissue.title")} onClick={() => onReissue(link)}>{t("guest.new")}</button>}
        {link.state !== "revoked" && <button type="button" className="btn small" disabled={busy} data-guest-edit={link.id} onClick={() => onEdit(link.id)}>{t("guest.edit")}</button>}
        {live && <button type="button" className="btn small danger" disabled={busy} onClick={() => onRevoke(link)}>{t("guest.revoke")}</button>}
      </div>
    </div>
  );
}

/** What all guests together may spend in a day, or no limit at all: a choice of its own, which the door stores
 *  as 0. The field says the sum already spent, and leaves the rest to the number. */
function DailyLimit({ door, toast }: { door: GuestDoor; toast: (text: string) => void }) {
  const [text, setText] = useState(String(door.usd_per_day || 5));
  const [none, setNone] = useState(!door.usd_per_day);
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // The door's own number follows while nobody is editing it, so a limit saved elsewhere shows up.
  useEffect(() => {
    if (dirty) return;
    setNone(!door.usd_per_day);
    if (door.usd_per_day) setText(String(door.usd_per_day));
  }, [door.usd_per_day, dirty]);
  const value = Number(text);
  const valid = none || (text.trim() !== "" && Number.isFinite(value) && value > 0 && value <= 100_000);
  async function save(unlimited = none) {
    if (!(unlimited || valid) || saving) return;
    setSaving(true);
    try {
      await api.put("/api/guest/settings", { usd_per_day: unlimited ? 0 : value });
      setDirty(false);
      invalidate("/api/guest/door");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setSaving(false);
    }
  }
  // "No limit" takes effect at once, like a switch: there is nothing to type, so nothing to save after.
  const toggle = () => {
    const next = !none;
    setNone(next);
    setDirty(!next);
    if (next) void save(true);
  };
  return (
    <div className="settings-row guest-daily" data-guest-daily="">
      <label className="settings-row-text" htmlFor="guest-daily">
        <span className="settings-row-title">{t("guest.daily")}</span>
        <span className="settings-row-desc">{t("guest.daily.spent", { sum: usd(door.spent_today) })}</span>
      </label>
      <div className="settings-row-ctl">
        {!none && (
          <>
            <span className="settings-num">
              <input id="guest-daily" className="field" type="number" inputMode="decimal" min={0} step="any" value={text} aria-invalid={!valid}
                onChange={(e) => { setText(e.target.value); setDirty(true); }} onKeyDown={(e) => { if (e.key === "Enter") void save(); }} />
              <span className="settings-unit">$</span>
            </span>
            <button type="button" className="btn small" disabled={!dirty || !valid || saving} onClick={() => void save()}>{t("common.save")}</button>
          </>
        )}
        <button type="button" className="chip select guest-nolimit" aria-pressed={none} data-guest-nolimit="usd_per_day" disabled={saving} onClick={toggle}>{t("guest.nolimit")}</button>
      </div>
    </div>
  );
}

// ── reading a conversation ───────────────────────────────────────────────────────────────

const NO_EXCHANGE: GuestExchange = { files: [], tasks: [], held: [] };

/** A door message's files, matched by the door's ids to what this side keeps of them. A file the door named and
 *  this side has no row for yet is left out rather than drawn as a name with nothing behind it. */
export function doorFiles(message: Pick<GuestMessage, "files">, files: readonly LinkFile[]): LinkFile[] {
  const ids = new Set(message.files ?? []);
  return ids.size ? files.filter((file) => file.remote_id && ids.has(file.remote_id)) : [];
}

function GuestReader({ id, sessionId, toast, onBack }: { id: string; sessionId: string; toast: (text: string) => void; onBack: () => void }) {
  const path = `${LINKS}/${encoded(id)}`;
  const query = useQuery<{ link: GuestLinkDetail; exchange?: GuestExchange }>(path, { pollMs: 4000 });
  const link = query.data?.link;
  const exchange = query.data?.exchange ?? NO_EXCHANGE;
  const [briefing, setBriefing] = useState(false);
  const [busy, setBusy] = useState(false);
  const refresh = () => {
    invalidate(path);
    invalidate(`/api/sessions/${encoded(sessionId)}/agent-talks`);
  };
  async function cancel(task: LinkTask) {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`${path}/tasks/${encoded(task.task)}/cancel`);
      refresh();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="guest-reader" data-guest-reader="">
      <div className="guest-bar">
        <button type="button" className="btn small ghost" onClick={onBack}><Icon name="back" size={14} />{t("guest.back")}</button>
        {link && <span className={`chip ${linkState(link).tone}`}>{t(`guest.state.${linkState(link).key}`)}</span>}
      </div>
      {!link ? (
        <div className="sub" role="status">{query.error ?? t("common.loading")}</div>
      ) : (
        <>
          <div className="guest-name">{link.label || link.topic || t("guest.untitled")}</div>
          {link.mode === "stand_in" && link.briefing && (
            <>
              <button type="button" className="btn small ghost guest-toggle" aria-expanded={briefing} onClick={() => setBriefing((open) => !open)}>{t("guest.briefing")}</button>
              {briefing && <div className="guest-briefing guest-msg-text" data-guest-briefing="">{link.briefing}</div>}
            </>
          )}
          {exchange.held.map((group) => <GuestHeldApproval key={group.batch} group={group} toast={toast} onDone={refresh} />)}
          <TaskList tasks={exchange.tasks} side="theirs" busy={busy} onCancel={cancel} />
          {link.messages.length === 0 ? <div className="sub">{t(link.mode === "direct" ? "guest.nomessages.direct" : "guest.nomessages")}</div> : link.messages.map((message) => <GuestMessageRow key={message.id} message={message} mode={link.mode} files={doorFiles(message, exchange.files)} toast={toast} />)}
        </>
      )}
    </div>
  );
}

function GuestMessageRow({ message, mode, files, toast }: { message: GuestMessage; mode: GuestMode; files: readonly LinkFile[]; toast: (text: string) => void }) {
  const who = message.role === "guest" ? t("guest.role.guest") : message.role === "door" ? t("guest.role.door") : t(mode === "direct" ? "guest.role.direct" : "guest.role.standin");
  const kind = message.kind === "file" && files.length ? "" : kindWord(message.kind, message.task_state);
  return (
    <div className="guest-msg" data-role={message.role} data-kind={message.kind || "text"}>
      <div className="guest-msg-head">
        <b>{who}</b>
        {kind && <span className="link-kind" data-link-kind={message.kind}>{kind}</span>}
        <span className="sub">{absTime(ms(message.at))}</span>
        {message.cost_usd > 0 && <span className="sub">{usd(message.cost_usd)}</span>}
      </div>
      {message.text && <div className="guest-msg-text">{message.text}</div>}
      <LinkFiles files={files} toast={toast} />
    </div>
  );
}

// ── making and editing a link ────────────────────────────────────────────────────────────

type NumberName = "usd_limit" | "turns_per_day" | "turns_total" | "invite_hours" | "conversation_hours" | "idle_hours" | "retention_days" | "file_mb" | "day_mb";
type LimitName = Exclude<NumberName, "retention_days">;
const NUMBERS: NumberName[] = ["usd_limit", "turns_per_day", "turns_total", "invite_hours", "conversation_hours", "idle_hours", "retention_days", "file_mb", "day_mb"];
const LIMITS: LimitName[] = ["usd_limit", "turns_per_day", "turns_total", "invite_hours", "conversation_hours", "idle_hours", "file_mb", "day_mb"];
/** The host's own defaults for files (`link_protocol.DEFAULT_FILE_MB`, `DEFAULT_DAY_MB`), for a door that names none. */
const FILE_DEFAULTS: Partial<Record<NumberName, number>> = { file_mb: 50, day_mb: 500 };
/** The limits only a direct link has: a stand-in takes no files, so its form neither shows nor checks them. */
const FILE_LIMITS: NumberName[] = ["file_mb", "day_mb"];

/** What the door accepts for a number that is set (`LinkBody` in daedalus/extensions/api_guest.py), so a value it
 *  would refuse is marked before it is sent, not after. "No limit" is a choice of its own and not a number: the
 *  door stores it as 0, which this form never asks anyone to type. */
const RANGES: Record<NumberName, { min: number; max: number; whole?: boolean; above?: boolean }> = {
  usd_limit: { min: 0, max: 10_000, above: true },
  turns_per_day: { min: 1, max: 100_000, whole: true },
  turns_total: { min: 1, max: 1_000_000, whole: true },
  invite_hours: { min: 0.1, max: 8760 },
  conversation_hours: { min: 0.1, max: 8760 },
  idle_hours: { min: 0.1, max: 8760 },
  retention_days: { min: 0, max: 3650 },
  file_mb: { min: 0, max: 1_000_000, above: true },
  day_mb: { min: 0, max: 1_000_000, above: true },
};

export function numberOf(name: NumberName, text: string): number | null {
  if (text.trim() === "") return null;
  const value = Number(text);
  const range = RANGES[name];
  if (!Number.isFinite(value) || value > range.max || (range.above ? value <= range.min : value < range.min) || (range.whole && !Number.isInteger(value))) return null;
  return value;
}

/** Who answers and how far they are trusted: the one choice the rest of the form follows. */
export type GuestLevel = "stand_in" | "direct" | "trusted";
const LEVELS: GuestLevel[] = ["stand_in", "direct", "trusted"];

export function levelOf(link: Pick<GuestLink, "mode" | "guest_tools">): GuestLevel {
  return link.mode === "stand_in" ? "stand_in" : link.guest_tools === "trusted" ? "trusted" : "direct";
}

type Numbers = { text: Record<NumberName, string>; none: Record<LimitName, boolean> };

/** A form's numbers from stored values: 0 is "no limit", shown as the choice with the default ready behind it. */
function numbersFrom(values: Record<NumberName, number>, defaults: GuestDefaults): Numbers {
  const text = {} as Record<NumberName, string>;
  const none = {} as Record<LimitName, boolean>;
  for (const name of NUMBERS) text[name] = String(name !== "retention_days" && !values[name] ? defaults[name] ?? FILE_DEFAULTS[name] ?? 0 : values[name]);
  for (const name of LIMITS) none[name] = !values[name];
  return { text, none };
}

/** How long an invite lives, in hours from when the link was made, as the door counts it; 0 for never. */
const inviteHours = (link: GuestLink) => (link.invite_expires_at ? Math.max(0.1, Math.round(((link.invite_expires_at - link.created_at) / 3600) * 10) / 10) : 0);

function LimitRow({ name, title, unit, numbers, never, onText, onNone }: {
  name: LimitName;
  title: string;
  unit?: string;
  numbers: Numbers;
  /** The word for no limit on a lifetime: it is not a limit there but an end. */
  never?: boolean;
  onText: (next: string) => void;
  onNone: (none: boolean) => void;
}) {
  const id = `guest-${name}`;
  const none = numbers.none[name];
  return (
    <div className="settings-row guest-limit" data-guest-limit={name} data-none={none || undefined}>
      <label className="settings-row-text" htmlFor={none ? undefined : id}><span className="settings-row-title">{title}</span></label>
      <div className="settings-row-ctl">
        {!none && (
          <span className="settings-num">
            <input id={id} className="field" type="number" inputMode="decimal" step="any" value={numbers.text[name]} aria-invalid={numberOf(name, numbers.text[name]) === null} onChange={(e) => onText(e.target.value)} />
            {unit && <span className="settings-unit">{unit}</span>}
          </span>
        )}
        <button type="button" className="chip select guest-nolimit" aria-pressed={none} data-guest-nolimit={name} onClick={() => onNone(!none)}>
          {t(never ? "guest.never" : "guest.nolimit")}
        </button>
      </div>
    </div>
  );
}

function NumberRow({ name, title, unit, value, onChange }: { name: NumberName; title: string; unit?: string; value: string; onChange: (next: string) => void }) {
  const id = `guest-${name}`;
  return (
    <div className="settings-row">
      <label className="settings-row-text" htmlFor={id}><span className="settings-row-title">{title}</span></label>
      <div className="settings-row-ctl">
        <span className="settings-num">
          <input id={id} className="field" type="number" inputMode="decimal" step="any" value={value} aria-invalid={numberOf(name, value) === null} onChange={(e) => onChange(e.target.value)} />
          <span className="settings-unit">{unit ?? ""}</span>
        </span>
      </div>
    </div>
  );
}

function priceHint(model: GuestDoor["models"][number]): string {
  if (model.billing === "plan") return t("guest.model.plan");
  if (model.free) return t("fmt.free");
  const price = (n: number) => `$${Number(n.toFixed(2))}`;
  return t("guest.model.price", { input: price(model.price_in), output: price(model.price_out) });
}

/** The form for a new link and for changing one: the same fields, so nothing can be set at making that cannot be
 *  changed afterwards. `editing` is the link as the host describes it, with what only the host keeps. */
function GuestForm({ sessionId, door, editing, onCancel, onMade, onSaved }: {
  sessionId: string;
  door: GuestDoor;
  editing?: GuestLinkDetail;
  onCancel: () => void;
  onMade: (url: string, topic: string) => void;
  onSaved: () => void;
}) {
  const defaults = door.defaults;
  const [level, setLevel] = useState<GuestLevel>(editing ? levelOf(editing) : defaults.mode === "direct" ? "direct" : "stand_in");
  const [label, setLabel] = useState(editing?.label ?? "");
  const [topic, setTopic] = useState(editing?.topic ?? "");
  const [instructions, setInstructions] = useState(editing?.instructions ?? "");
  // The cheapest model first: it is what a stranger's questions should cost by default.
  const [modelId, setModelId] = useState(editing?.model_id || door.models[0]?.id || "");
  const [briefing, setBriefing] = useState(editing?.briefing ?? "");
  // A briefing written by the model is the one thing here the operator has not written, and it is
  // published to whoever holds the link; so it is read and ticked before it can be used.
  const [drafted, setDrafted] = useState(false);
  const [read, setRead] = useState(false);
  const [tools, setTools] = useState<GuestTools>(editing && editing.guest_tools !== "trusted" && editing.mode === "direct" ? editing.guest_tools : defaults.tools);
  const [approval, setApproval] = useState(editing?.approval ?? defaults.approval);
  const [numbers, setNumbers] = useState<Numbers>(() => numbersFrom(editing ? { ...editing, invite_hours: inviteHours(editing) } : defaults, defaults));
  // Moving to Trusted lifts the limits, and moving off it puts the defaults back, until the operator sets one
  // by hand: from then on the numbers are theirs and a change of level leaves them alone.
  const [touched, setTouched] = useState(!!editing);
  const [drafting, setDrafting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const labelField = useRef<HTMLInputElement>(null);
  useEffect(() => {
    if (!editing) labelField.current?.focus();
  }, [editing]);

  const setText = (name: NumberName) => (next: string) => {
    setTouched(true);
    setNumbers((current) => ({ ...current, text: { ...current.text, [name]: next } }));
  };
  const setNone = (name: LimitName) => (none: boolean) => {
    setTouched(true);
    setNumbers((current) => ({ ...current, none: { ...current.none, [name]: none } }));
  };
  function choose(next: GuestLevel) {
    setLevel(next);
    setProblem("");
    if (!touched) setNumbers(numbersFrom(next === "trusted" ? { ...defaults, ...door.trusted_defaults } : defaults, defaults));
  }

  const standIn = level === "stand_in";
  // What bounds this link: dollars only where the model counts them, so no field stands there doing nothing.
  const billing: GuestBilling = standIn ? door.models.find((model) => model.id === modelId)?.billing ?? "metered" : door.chat_priced ? "metered" : "plan";
  const priced = billing === "metered";
  const value = (name: NumberName): number | null => (name !== "retention_days" && numbers.none[name] ? 0 : numberOf(name, numbers.text[name]));
  const valid = NUMBERS.filter((name) => (priced || name !== "usd_limit") && (!standIn || !FILE_LIMITS.includes(name))).every((name) => value(name) !== null)
    && (!standIn || (!!modelId && briefing.trim() !== "" && (!drafted || read)));

  function writeBriefing(next: string) {
    setBriefing(next);
    setRead(false);
    if (!next.trim()) setDrafted(false);
  }

  async function draft() {
    if (drafting) return;
    if (briefing.trim() && !drafted && !(await confirmAsync(t("guest.draft.replace"), { body: t("guest.draft.replace.body"), action: t("guest.draft.replace.action"), danger: false }))) return;
    setDrafting(true);
    setProblem("");
    try {
      const out = await api.post<{ briefing: string }>(`/api/sessions/${encoded(sessionId)}/guest/draft`);
      const text = (out.briefing ?? "").trim();
      if (!text) {
        setProblem(t("guest.draft.empty"));
      } else {
        setBriefing(text);
        setDrafted(true);
        setRead(false);
      }
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setDrafting(false);
    }
  }

  // The door says whether this chat can be given the sandboxed tools; when it cannot, that choice is not
  // offered at all (a greyed segment would invite the question) and one line says why.
  const toolOptions: { id: GuestTools; label: string }[] = [
    { id: "none", label: t("guest.tools.none") },
    { id: "read", label: t("guest.tools.read") },
    ...(door.full_tools ? [{ id: "all" as const, label: t("guest.tools.all") }] : []),
  ];
  // Never `all` while the door says it is not available, whatever was chosen before it said so.
  const chosenTools: GuestTools = level === "trusted" ? "trusted" : tools === "all" && !door.full_tools ? "read" : tools === "trusted" ? "read" : tools;

  async function submit() {
    if (!valid || busy) return;
    setBusy(true);
    setProblem("");
    const body: GuestLinkBody = {
      label: label.trim(),
      topic: topic.trim(),
      mode: standIn ? "stand_in" : "direct",
      model_id: standIn ? modelId : "",
      briefing: standIn ? briefing : "",
      instructions: standIn ? "" : instructions.trim(),
      // Reaching this point means the briefing was typed, or was drafted and then ticked as read.
      briefing_approved: true,
      tools: standIn ? "none" : chosenTools,
      approval,
      usd_limit: priced ? value("usd_limit")! : 0,
      turns_per_day: value("turns_per_day")!,
      turns_total: value("turns_total")!,
      invite_hours: value("invite_hours")!,
      conversation_hours: value("conversation_hours")!,
      idle_hours: value("idle_hours")!,
      retention_days: value("retention_days")!,
      // A stand-in takes no files; it is sent what it has, so a link made as one and turned direct later keeps them.
      file_mb: standIn ? (editing?.file_mb ?? defaults.file_mb ?? FILE_DEFAULTS.file_mb!) : value("file_mb")!,
      day_mb: standIn ? (editing?.day_mb ?? defaults.day_mb ?? FILE_DEFAULTS.day_mb!) : value("day_mb")!,
    };
    try {
      if (editing) {
        await api.put(`${LINKS}/${encoded(editing.id)}`, body);
        invalidate("/api/guest/");
        onSaved();
      } else {
        const made = await api.post<GuestInvite>(`/api/sessions/${encoded(sessionId)}/guest/links`, body);
        invalidate("/api/guest/");
        onMade(made.url, body.topic);
      }
    } catch (e) {
      setProblem(errorText(e));
      setBusy(false);
    }
  }

  return (
    <form className="guest-form" data-guest-form={editing ? "edit" : "new"} onSubmit={(e) => { e.preventDefault(); void submit(); }}>
      {!editing && <AddressNote text={t("address.guest.local")} />}
      <div className="access-options guest-levels" role="radiogroup" aria-label={t("guest.level")}>
        {LEVELS.map((item) => (
          <label key={item} className={`access-option ${level === item ? "on" : ""}`} data-guest-level={item}>
            <input type="radio" name="guest-level" checked={level === item} onChange={() => choose(item)} />
            <span>
              <b>{t(`guest.level.${item}`)}</b>
              <span className="sub">{t(`guest.level.${item}.hint`)}</span>
            </span>
          </label>
        ))}
      </div>
      {level === "trusted" && <div className="sub guest-warning" data-guest-warning="trusted"><Icon name="alert" size={13} />{t("guest.trusted.warning")}</div>}

      <label className="field" htmlFor="guest-label">{t("guest.field.label")}</label>
      <input id="guest-label" ref={labelField} className="field" value={label} maxLength={120} placeholder={t("guest.field.label.ph")} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
      <label className="field" htmlFor="guest-topic">{t(standIn ? "guest.field.topic" : "guest.field.topic.direct")}</label>
      <input id="guest-topic" className="field" value={topic} maxLength={240} placeholder={t("guest.field.topic.ph")} autoComplete="off" onChange={(e) => setTopic(e.target.value)} />

      {standIn ? (
        <>
          {door.models.length === 0 ? (
            <div className="sub guest-problem" data-guest-nomodel="">{t("guest.nomodel")}</div>
          ) : (
            <div className="settings-row">
              <span className="settings-row-text"><span className="settings-row-title">{t("guest.field.model")}</span></span>
              <div className="settings-row-ctl">
                <Dropdown value={modelId} options={door.models.map((model) => ({ id: model.id, label: model.label, hint: priceHint(model) }))} onChange={setModelId} label={t("guest.field.model")} id="guest-model" />
              </div>
            </div>
          )}
          <label className="field" htmlFor="guest-briefing">{t("guest.field.briefing")}</label>
          <textarea id="guest-briefing" className="field" rows={5} maxLength={20_000} value={briefing} placeholder={t("guest.field.briefing.ph")} onChange={(e) => writeBriefing(e.target.value)} />
          <button type="button" className="btn small guest-draft" data-guest-draft="" disabled={drafting} onClick={() => void draft()}>
            <Icon name="wand" size={14} />{t(drafting ? "guest.drafting" : "guest.draft")}
          </button>
          {drafted && (
            <label className="toggle-row" data-guest-read="">
              <input type="checkbox" checked={read} onChange={(e) => setRead(e.target.checked)} />
              <span>{t("guest.read.tick")}</span>
            </label>
          )}
        </>
      ) : (
        <>
          {level === "direct" && (
            <>
              <label className="field">{t("guest.field.tools")}</label>
              <Segmented value={chosenTools} options={toolOptions} onChange={setTools} label={t("guest.field.tools")} />
              {!door.full_tools && <div className="sub" data-guest-tools-why={door.full_tools_why || "other"}>{t(door.full_tools_why === "no_sandbox" || door.full_tools_why === "no_limits" || door.full_tools_why === "host_folder" || door.full_tools_why === "project_folder" ? `guest.tools.why.${door.full_tools_why}` : "guest.tools.why.other")}</div>}
              {chosenTools === "all" && <div className="sub" data-guest-tools-all="">{t("guest.tools.all.note")}</div>}
            </>
          )}
          <label className="field" htmlFor="guest-instructions">{t("guest.field.instructions")}</label>
          <textarea id="guest-instructions" className="field" rows={2} maxLength={4000} value={instructions} placeholder={t("guest.field.instructions.ph")} onChange={(e) => setInstructions(e.target.value)} />
        </>
      )}

      <div className="guest-section" data-guest-limits="">{t("guest.limits")}</div>
      {priced
        ? <LimitRow name="usd_limit" title={t("guest.field.usd")} unit="$" numbers={numbers} onText={setText("usd_limit")} onNone={setNone("usd_limit")} />
        : <div className="sub" data-guest-bound={billing}>{t(`guest.bound.${billing}`)}</div>}
      <LimitRow name="turns_per_day" title={t("guest.field.turnsday")} numbers={numbers} onText={setText("turns_per_day")} onNone={setNone("turns_per_day")} />
      <LimitRow name="invite_hours" title={t("guest.field.lifetime")} unit={t("guest.unit.hours")} never numbers={numbers} onText={setText("invite_hours")} onNone={setNone("invite_hours")} />
      {!standIn && (
        <>
          <LimitRow name="file_mb" title={t("link.limit.file")} unit={t("link.unit.mb")} numbers={numbers} onText={setText("file_mb")} onNone={setNone("file_mb")} />
          <LimitRow name="day_mb" title={t("link.limit.day")} unit={t("link.unit.mb")} numbers={numbers} onText={setText("day_mb")} onNone={setNone("day_mb")} />
        </>
      )}
      <div className="settings-row">
        <span className="settings-row-text"><span className="settings-row-title">{t("guest.field.approval")}</span></span>
        <div className="settings-row-ctl"><Switch checked={approval} onChange={setApproval} label={t("guest.field.approval")} /></div>
      </div>
      <details className="guest-more" data-guest-more="">
        <summary>{t("guest.more")}</summary>
        <LimitRow name="turns_total" title={t("guest.field.turnstotal")} numbers={numbers} onText={setText("turns_total")} onNone={setNone("turns_total")} />
        <LimitRow name="conversation_hours" title={t("guest.field.conversation")} unit={t("guest.unit.hours")} never numbers={numbers} onText={setText("conversation_hours")} onNone={setNone("conversation_hours")} />
        <LimitRow name="idle_hours" title={t("guest.field.idle")} unit={t("guest.unit.hours")} never numbers={numbers} onText={setText("idle_hours")} onNone={setNone("idle_hours")} />
        <NumberRow name="retention_days" title={t(standIn ? "guest.field.retention" : "guest.field.retention.direct")} unit={t("guest.unit.days")} value={numbers.text.retention_days} onChange={setText("retention_days")} />
      </details>
      {problem && <div className="sub guest-problem" role="alert" data-guest-problem="">{problem}</div>}
      <div className="sheet-foot">
        <button type="button" className="btn ghost" onClick={onCancel}>{t("common.cancel")}</button>
        <button type="submit" className="btn primary" data-guest-create="" disabled={!valid || busy}>{t(editing ? "common.save" : "guest.create")}</button>
      </div>
    </form>
  );
}

/** The form opened on a link's own settings, read from the host so that what only the host keeps comes too. */
function GuestEditor({ id, sessionId, door, onBack, onSaved }: { id: string; sessionId: string; door: GuestDoor; onBack: () => void; onSaved: () => void }) {
  // Read afresh and once, past the cache: the form starts from what it is given and is the operator's while it
  // is open, so a copy the reader left behind would be what got saved back.
  const [link, setLink] = useState<GuestLinkDetail | null>(null);
  const [failed, setFailed] = useState("");
  useEffect(() => {
    let live = true;
    api.get<{ link: GuestLinkDetail }>(`${LINKS}/${encoded(id)}`).then((out) => { if (live) setLink(out.link); }, (e) => { if (live) setFailed(errorText(e)); });
    return () => { live = false; };
  }, [id]);
  if (!link) return <div className="sub" role="status">{failed || t("common.loading")}</div>;
  return <GuestForm sessionId={sessionId} door={door} editing={link} onCancel={onBack} onMade={() => {}} onSaved={onSaved} />;
}

// ── the link, once ───────────────────────────────────────────────────────────────────────

/** The address exists nowhere but here: not in the list, not on the door, not in this app's cache. Closing
 *  the sheet is the last time anyone sees it. */
function MadeLink({ url, topic, toast, onDone }: { url: string; topic: string; toast: (text: string) => void; onDone: () => void }) {
  const text = inviteText(topic, url);
  const [copied, copyInvite] = useCopied();
  async function copy() {
    if (!(await copyInvite(text))) toast(t("svc.copyfail"));
  }
  return (
    <div className="guest-made" data-guest-made="">
      <label className="field" htmlFor="guest-url">{t("guest.made")}</label>
      {/* The whole text, wrapped: an address cut short at the field's edge looks broken and cannot be checked. */}
      <textarea id="guest-url" className="field mono" readOnly rows={3} value={text} onFocus={(e) => e.target.select()} />
      <button type="button" className="btn primary guest-copy" data-guest-copy="" onClick={() => void copy()}><Icon name={copied ? "check" : "copy"} size={13} /> {t("common.copy")}</button>
      <div className="sub attn" data-guest-once="">{t("guest.once")}</div>
      <div className="sheet-foot">
        <button type="button" className="btn primary" onClick={onDone}>{t("guest.done")}</button>
      </div>
    </div>
  );
}

// ── in the chat ──────────────────────────────────────────────────────────────────────────

/** A guest's message in the conversation: a card with their mark and what their turn could do. Plain
 *  text only: it is a stranger's, and it is drawn on the answer's side, never as the operator's bubble. */
export function GuestCard({ guest, text }: { guest?: { label: string; tools: string; kind?: "reply" } | null; text: string }) {
  const label = guest?.label ?? "";
  const reply = guest?.kind === "reply";
  const tools = reply ? t("talk.card.woke") : guest?.tools === "none" || guest?.tools === "read" || guest?.tools === "all" || guest?.tools === "trusted" ? t(`guest.tools.${guest.tools}`) : "";
  return (
    <div className="guest-card" data-from-guest={label || "guest"} data-reply={reply || undefined}>
      <div className="guest-card-head">
        <Icon name="link" size={13} />
        <b>{reply ? t("talk.card.from", { host: label }) : label ? t("guest.from", { label }) : t("guest.from.none")}</b>
        {tools && <span>{tools}</span>}
      </div>
      <div className="guest-card-text">{text}</div>
    </div>
  );
}
