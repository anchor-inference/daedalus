// The agent link: the sheet a chat opens to hand someone a link, and the card a guest's message is drawn
// as in the chat. A guest is a stranger, so everything they wrote is shown as plain text, in a frame
// that says whose it is, and nothing here turns it into markdown, a link or a picture.

import { useEffect, useRef, useState } from "react";
import { api, GuestDoor, GuestInvite, GuestLink, GuestLinkBody, GuestLinkDetail, GuestMessage, GuestMode, GuestTools } from "./api";
import { absTime, relTimeLong, usd } from "./format";
import { Icon } from "./icons";
import { plural, t } from "./i18n";
import { invalidate, useQuery } from "./store";
import { copyText, Dropdown, Segmented, Switch } from "./ui/components";
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

type View = { kind: "list" } | { kind: "form" } | { kind: "made"; url: string; topic: string } | { kind: "read"; id: string };

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
          <div className="sub" data-guest-off="address">{t("guest.noaddress")}</div>
        ) : view.kind === "form" ? (
          <GuestForm sessionId={sessionId} door={data} onCancel={() => setView({ kind: "list" })} onMade={(url, topic) => setView({ kind: "made", url, topic })} />
        ) : view.kind === "made" ? (
          <MadeLink url={view.url} topic={view.topic} toast={toast} onDone={() => setView({ kind: "list" })} />
        ) : view.kind === "read" ? (
          <GuestReader id={view.id} onBack={() => setView({ kind: "list" })} />
        ) : (
          <GuestList sessionId={sessionId} door={data} toast={toast} onNew={() => setView({ kind: "form" })} onRead={(id) => setView({ kind: "read", id })} onMade={(url, topic) => setView({ kind: "made", url, topic })} />
        )}
      </div>
    </Sheet>
  );
}

// ── the list ─────────────────────────────────────────────────────────────────────────────

function GuestList({ sessionId, door, toast, onNew, onRead, onMade }: {
  sessionId: string;
  door: GuestDoor;
  toast: (text: string) => void;
  onNew: () => void;
  onRead: (id: string) => void;
  onMade: (url: string, topic: string) => void;
}) {
  const links = useQuery<{ links: GuestLink[] }>(`${LINKS}?session_id=${encoded(sessionId)}`, { pollMs: POLL_MS });
  const [busy, setBusy] = useState<string | null>(null);

  async function act(key: string, work: () => Promise<string | void>) {
    if (busy) return;
    setBusy(key);
    try {
      const said = await work();
      invalidate("/api/guest/");
      if (said) toast(said);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(null);
    }
  }

  const decide = (link: GuestLink, decision: "admit" | "refuse") => act(`${decision}:${link.id}`, async () => {
    await api.post(`/api/guest/conversations/${encoded(link.conversation!.id)}/${decision}`);
    return t(decision === "admit" ? "guest.admitted" : "guest.refused");
  });
  const revoke = (link: GuestLink) => act(`revoke:${link.id}`, async () => {
    await api.post(`${LINKS}/${encoded(link.id)}/revoke`);
    return t("guest.revoked");
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
            <GuestLinkCard key={link.id} link={link} busy={busy !== null} onDecide={decide} onRevoke={revoke} onReissue={reissue} onRead={onRead} />
          ))}
        </div>
      )}
      <DailyLimit door={door} toast={toast} />
    </>
  );
}

function GuestLinkCard({ link, busy, onDecide, onRevoke, onReissue, onRead }: {
  link: GuestLink;
  busy: boolean;
  onDecide: (link: GuestLink, decision: "admit" | "refuse") => void;
  onRevoke: (link: GuestLink) => void;
  onReissue: (link: GuestLink) => void;
  onRead: (id: string) => void;
}) {
  const state = linkState(link);
  const conversation = link.conversation;
  const pending = conversation?.state === "pending";
  const live = link.state === "waiting" || link.state === "spent";
  const meta = [
    t(`guest.mode.${link.mode}`),
    link.mode === "stand_in" ? link.model_name : t(`guest.tools.${link.guest_tools}`),
    `${usd(link.spent_usd)} / ${usd(link.usd_limit)}`,
    plural("guest.turns", link.turns_used),
    link.state === "waiting" ? t("guest.expires", { when: relTimeLong(ms(link.invite_expires_at)) }) : "",
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
        {live && <button type="button" className="btn small danger" disabled={busy} onClick={() => onRevoke(link)}>{t("guest.revoke")}</button>}
      </div>
    </div>
  );
}

/** What all guests together may spend in a day. Zero shuts them out: the field says what it is and the
 *  sum already spent, and leaves the rest to the number. */
function DailyLimit({ door, toast }: { door: GuestDoor; toast: (text: string) => void }) {
  const [text, setText] = useState(String(door.usd_per_day));
  const [dirty, setDirty] = useState(false);
  const [saving, setSaving] = useState(false);
  // The door's own number follows while nobody is editing it, so a limit saved elsewhere shows up.
  useEffect(() => {
    if (!dirty) setText(String(door.usd_per_day));
  }, [door.usd_per_day, dirty]);
  const value = Number(text);
  const valid = text.trim() !== "" && Number.isFinite(value) && value >= 0 && value <= 100_000;
  async function save() {
    if (!valid || saving) return;
    setSaving(true);
    try {
      await api.put("/api/guest/settings", { usd_per_day: value });
      setDirty(false);
      invalidate("/api/guest/door");
      toast(t("guest.daily.saved"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setSaving(false);
    }
  }
  return (
    <div className="settings-row guest-daily" data-guest-daily="">
      <label className="settings-row-text" htmlFor="guest-daily">
        <span className="settings-row-title">{t("guest.daily")}</span>
        <span className="settings-row-desc">{t("guest.daily.spent", { sum: usd(door.spent_today) })}</span>
      </label>
      <div className="settings-row-ctl">
        <span className="settings-num">
          <input id="guest-daily" className="field" type="number" inputMode="decimal" min={0} step="any" value={text} aria-invalid={!valid}
            onChange={(e) => { setText(e.target.value); setDirty(true); }} onKeyDown={(e) => { if (e.key === "Enter") void save(); }} />
          <span className="settings-unit">$</span>
        </span>
        <button type="button" className="btn small" disabled={!dirty || !valid || saving} onClick={() => void save()}>{t("common.save")}</button>
      </div>
    </div>
  );
}

// ── reading a conversation ───────────────────────────────────────────────────────────────

function GuestReader({ id, onBack }: { id: string; onBack: () => void }) {
  const query = useQuery<{ link: GuestLinkDetail }>(`${LINKS}/${encoded(id)}`, { pollMs: 4000 });
  const link = query.data?.link;
  const [briefing, setBriefing] = useState(false);
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
          {link.messages.length === 0 ? <div className="sub">{t(link.mode === "direct" ? "guest.nomessages.direct" : "guest.nomessages")}</div> : link.messages.map((message) => <GuestMessageRow key={message.id} message={message} mode={link.mode} />)}
        </>
      )}
    </div>
  );
}

function GuestMessageRow({ message, mode }: { message: GuestMessage; mode: GuestMode }) {
  const who = message.role === "guest" ? t("guest.role.guest") : message.role === "door" ? t("guest.role.door") : t(mode === "direct" ? "guest.role.direct" : "guest.role.standin");
  return (
    <div className="guest-msg" data-role={message.role}>
      <div className="guest-msg-head">
        <b>{who}</b>
        <span className="sub">{absTime(ms(message.at))}</span>
        {message.cost_usd > 0 && <span className="sub">{usd(message.cost_usd)}</span>}
      </div>
      <div className="guest-msg-text">{message.text}</div>
    </div>
  );
}

// ── making a link ────────────────────────────────────────────────────────────────────────

type NumberName = "usd_limit" | "turns_per_day" | "turns_total" | "invite_hours" | "conversation_hours" | "idle_hours" | "retention_days";

/** What the door accepts for each number (`LinkBody` in daedalus/extensions/api_guest.py), so a value it
 *  would refuse is marked before it is sent, not after. */
const RANGES: Record<NumberName, { min: number; max: number; whole?: boolean; above?: boolean }> = {
  usd_limit: { min: 0, max: 10_000 },
  turns_per_day: { min: 1, max: 100_000, whole: true },
  turns_total: { min: 1, max: 1_000_000, whole: true },
  invite_hours: { min: 0, max: 8760, above: true },
  conversation_hours: { min: 0, max: 8760, above: true },
  idle_hours: { min: 0, max: 8760, above: true },
  retention_days: { min: 0, max: 3650 },
};

export function numberOf(name: NumberName, text: string): number | null {
  if (text.trim() === "") return null;
  const value = Number(text);
  const range = RANGES[name];
  if (!Number.isFinite(value) || value > range.max || (range.above ? value <= range.min : value < range.min) || (range.whole && !Number.isInteger(value))) return null;
  return value;
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
  if (model.free) return t("fmt.free");
  const price = (n: number) => `$${Number(n.toFixed(2))}`;
  return t("guest.model.price", { input: price(model.price_in), output: price(model.price_out) });
}

function GuestForm({ sessionId, door, onCancel, onMade }: { sessionId: string; door: GuestDoor; onCancel: () => void; onMade: (url: string, topic: string) => void }) {
  const defaults = door.defaults;
  const [mode, setMode] = useState<GuestMode>(defaults.mode);
  const [label, setLabel] = useState("");
  const [topic, setTopic] = useState("");
  // The cheapest model first: it is what a stranger's questions should cost by default.
  const [modelId, setModelId] = useState(door.models[0]?.id ?? "");
  const [briefing, setBriefing] = useState("");
  // A briefing written by the model is the one thing here the operator has not written, and it is
  // published to whoever holds the link; so it is read and ticked before it can be used.
  const [drafted, setDrafted] = useState(false);
  const [read, setRead] = useState(false);
  const [tools, setTools] = useState<GuestTools>(defaults.tools);
  const [approval, setApproval] = useState(defaults.approval);
  const [numbers, setNumbers] = useState<Record<NumberName, string>>({
    usd_limit: String(defaults.usd_limit),
    turns_per_day: String(defaults.turns_per_day),
    turns_total: String(defaults.turns_total),
    invite_hours: String(defaults.invite_hours),
    conversation_hours: String(defaults.conversation_hours),
    idle_hours: String(defaults.idle_hours),
    retention_days: String(defaults.retention_days),
  });
  const [drafting, setDrafting] = useState(false);
  const [busy, setBusy] = useState(false);
  const [problem, setProblem] = useState("");
  const labelField = useRef<HTMLInputElement>(null);
  useEffect(() => {
    labelField.current?.focus();
  }, []);

  const set = (name: NumberName) => (next: string) => setNumbers((current) => ({ ...current, [name]: next }));
  const standIn = mode === "stand_in";
  const parsed = (Object.keys(numbers) as NumberName[]).map((name) => numberOf(name, numbers[name]));
  const valid = parsed.every((value) => value !== null)
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

  async function create() {
    if (!valid || busy) return;
    setBusy(true);
    setProblem("");
    const body: GuestLinkBody = {
      label: label.trim(),
      topic: topic.trim(),
      mode,
      model_id: standIn ? modelId : "",
      briefing: standIn ? briefing : "",
      // Reaching this point means the briefing was typed, or was drafted and then ticked as read.
      briefing_approved: true,
      tools: chosenTools,
      approval,
      usd_limit: numberOf("usd_limit", numbers.usd_limit)!,
      turns_per_day: numberOf("turns_per_day", numbers.turns_per_day)!,
      turns_total: numberOf("turns_total", numbers.turns_total)!,
      invite_hours: numberOf("invite_hours", numbers.invite_hours)!,
      conversation_hours: numberOf("conversation_hours", numbers.conversation_hours)!,
      idle_hours: numberOf("idle_hours", numbers.idle_hours)!,
      retention_days: numberOf("retention_days", numbers.retention_days)!,
    };
    try {
      const made = await api.post<GuestInvite>(`/api/sessions/${encoded(sessionId)}/guest/links`, body);
      invalidate("/api/guest/");
      onMade(made.url, body.topic);
    } catch (e) {
      setProblem(errorText(e));
      setBusy(false);
    }
  }

  const modeOptions: { id: GuestMode; label: string }[] = [
    { id: "stand_in", label: t("guest.mode.stand_in") },
    { id: "direct", label: t("guest.mode.direct") },
  ];
  // The door says whether this chat can be given the sandboxed tools; when it cannot, that choice is not
  // offered at all (a greyed segment would invite the question) and one line says why.
  const toolOptions: { id: GuestTools; label: string }[] = [
    { id: "none", label: t("guest.tools.none") },
    { id: "read", label: t("guest.tools.read") },
    ...(door.full_tools ? [{ id: "all" as const, label: t("guest.tools.all") }] : []),
  ];
  // Never `all` while the door says it is not available, whatever was chosen before it said so.
  const chosenTools: GuestTools = tools === "all" && !door.full_tools ? "read" : tools;
  return (
    <form className="guest-form" data-guest-form="" onSubmit={(e) => { e.preventDefault(); void create(); }}>
      <label className="field">{t("guest.field.mode")}</label>
      <Segmented value={mode} options={modeOptions} onChange={(next) => { setMode(next); setProblem(""); }} label={t("guest.field.mode")} />
      {standIn
        ? <div className="sub" data-guest-note="standin">{t("guest.standin.note")}</div>
        : <div className="sub guest-warning" data-guest-warning="">{t(door.direct ? "guest.direct.warning" : "guest.direct.private")}</div>}

      <label className="field" htmlFor="guest-label">{t("guest.field.label")}</label>
      <input id="guest-label" ref={labelField} className="field" value={label} maxLength={120} placeholder={t("guest.field.label.ph")} autoComplete="off" onChange={(e) => setLabel(e.target.value)} />
      <label className="field" htmlFor="guest-topic">{t("guest.field.topic")}</label>
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
          <textarea id="guest-briefing" className="field" rows={6} maxLength={20_000} value={briefing} placeholder={t("guest.field.briefing.ph")} onChange={(e) => writeBriefing(e.target.value)} />
          <div className="sub">{t("guest.briefing.note")}</div>
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
          <label className="field">{t("guest.field.tools")}</label>
          <Segmented value={chosenTools} options={toolOptions} onChange={setTools} label={t("guest.field.tools")} />
          {!door.full_tools && <div className="sub" data-guest-tools-why={door.full_tools_why || "other"}>{t(door.full_tools_why === "no_sandbox" || door.full_tools_why === "host_folder" || door.full_tools_why === "project_folder" ? `guest.tools.why.${door.full_tools_why}` : "guest.tools.why.other")}</div>}
          {chosenTools === "all" && <div className="sub" data-guest-tools-all="">{t("guest.tools.all.note")}</div>}
        </>
      )}

      <NumberRow name="invite_hours" title={t("guest.field.lifetime")} unit={t("guest.unit.hours")} value={numbers.invite_hours} onChange={set("invite_hours")} />
      <NumberRow name="usd_limit" title={t("guest.field.usd")} unit="$" value={numbers.usd_limit} onChange={set("usd_limit")} />
      <NumberRow name="turns_per_day" title={t("guest.field.turnsday")} value={numbers.turns_per_day} onChange={set("turns_per_day")} />
      <div className="settings-row">
        <span className="settings-row-text"><span className="settings-row-title">{t("guest.field.approval")}</span></span>
        <div className="settings-row-ctl"><Switch checked={approval} onChange={setApproval} label={t("guest.field.approval")} /></div>
      </div>
      <details className="guest-more" data-guest-more="">
        <summary>{t("guest.more")}</summary>
        <NumberRow name="turns_total" title={t("guest.field.turnstotal")} value={numbers.turns_total} onChange={set("turns_total")} />
        <NumberRow name="conversation_hours" title={t("guest.field.conversation")} unit={t("guest.unit.hours")} value={numbers.conversation_hours} onChange={set("conversation_hours")} />
        <NumberRow name="idle_hours" title={t("guest.field.idle")} unit={t("guest.unit.hours")} value={numbers.idle_hours} onChange={set("idle_hours")} />
        <NumberRow name="retention_days" title={t(standIn ? "guest.field.retention" : "guest.field.retention.direct")} unit={t("guest.unit.days")} value={numbers.retention_days} onChange={set("retention_days")} />
      </details>
      {problem && <div className="sub guest-problem" role="alert" data-guest-problem="">{problem}</div>}
      <div className="sheet-foot">
        <button type="button" className="btn ghost" onClick={onCancel}>{t("common.cancel")}</button>
        <button type="submit" className="btn primary" data-guest-create="" disabled={!valid || busy}>{t("guest.create")}</button>
      </div>
    </form>
  );
}

// ── the link, once ───────────────────────────────────────────────────────────────────────

/** The address exists nowhere but here: not in the list, not on the door, not in this app's cache. Closing
 *  the sheet is the last time anyone sees it. */
function MadeLink({ url, topic, toast, onDone }: { url: string; topic: string; toast: (text: string) => void; onDone: () => void }) {
  const text = inviteText(topic, url);
  async function copy() {
    toast(t((await copyText(text)) ? "guest.copied" : "svc.copyfail"));
  }
  return (
    <div className="guest-made" data-guest-made="">
      <label className="field" htmlFor="guest-url">{t("guest.made")}</label>
      {/* The whole text, wrapped: an address cut short at the field's edge looks broken and cannot be checked. */}
      <textarea id="guest-url" className="field mono" readOnly rows={3} value={text} onFocus={(e) => e.target.select()} />
      <button type="button" className="btn primary guest-copy" data-guest-copy="" onClick={() => void copy()}><Icon name="copy" size={13} /> {t("common.copy")}</button>
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
export function GuestCard({ guest, text }: { guest?: { label: string; tools: string } | null; text: string }) {
  const label = guest?.label ?? "";
  const tools = guest?.tools === "none" || guest?.tools === "read" || guest?.tools === "all" ? t(`guest.tools.${guest.tools}`) : "";
  return (
    <div className="guest-card" data-from-guest={label || "guest"}>
      <div className="guest-card-head">
        <Icon name="link" size={13} />
        <b>{label ? t("guest.from", { label }) : t("guest.from.none")}</b>
        {tools && <span>{tools}</span>}
      </div>
      <div className="guest-card-text">{text}</div>
    </div>
  );
}
