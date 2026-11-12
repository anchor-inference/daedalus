// Standing contacts with other installations, and the operator's own words to the other side: the Contacts block at
// the head of the Agents tab, the offer of a contact from a conversation that exists, and the small composer that
// sends a message as the operator rather than as the agent.
//
// What the other side wrote (the name it offered itself under, the words of its offer) is plain text here and nothing
// else. A key is shown only as its fingerprint, which is what two operators read to each other to know whose it is.

import { useState } from "react";
import { api, Contact, ContactList } from "./api";
import { BackgroundNote } from "./backgroundnote";
import { relTime, usd } from "./format";
import { Icon } from "./icons";
import { t } from "./i18n";
import { invalidate, useQuery } from "./store";
import { Segmented } from "./ui/components";
import { confirmAsync, errorText } from "./ui";
import "./contacts.css";

export const CONTACTS = "/api/contacts";
const POLL_MS = 8000;
const encoded = encodeURIComponent;

export type ContactLevel = "read" | "all" | "trusted";
const LEVELS: ContactLevel[] = ["read", "all", "trusted"];

/** The word and tone a contact is shown with: `attn` where it waits for the operator or something is wrong with its key. */
export function contactState(contact: Pick<Contact, "state" | "reason">): { key: string; tone: "" | "attn" | "ok" } {
  if (contact.state === "incoming") return { key: "incoming", tone: "attn" };
  if (contact.state === "broken") return { key: "broken", tone: "" };
  if (contact.reason === "key") return { key: "key", tone: "attn" };
  if (contact.state === "active") return { key: "active", tone: "ok" };
  return { key: contact.state, tone: "" };
}

/** A fingerprint, or the word that says there is none to compare. */
export function KeyLine({ fingerprint, verified = !!fingerprint }: { fingerprint?: string; verified?: boolean }) {
  return verified && fingerprint ? (
    <span className="key-line" data-key="verified" title={t("contact.key.hint")}><Icon name="lock" size={12} />{t("contact.key.verified", { fp: fingerprint })}</span>
  ) : (
    <span className="key-line" data-key="unverified">{t("contact.key.unverified")}</span>
  );
}

/** A message as the operator, not the agent: one field and Send. The other side reads it marked as the owner's. */
export function OwnerSay({ path, onSent, toast }: { path: string; onSent: () => void; toast: (text: string) => void }) {
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  async function send() {
    const words = text.trim();
    if (!words || busy) return;
    setBusy(true);
    try {
      await api.post(path, { text: words });
      setText("");
      onSent();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="owner-say" data-owner-say="">
      <textarea className="field" rows={2} maxLength={8000} value={text} aria-label={t("owner.say")} placeholder={t("owner.say.ph")} disabled={busy}
        onChange={(e) => setText(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) void send(); }} />
      <button type="button" className="btn small" data-owner-send="" disabled={busy || !text.trim()} onClick={() => void send()}>{t("owner.send")}</button>
    </div>
  );
}

/** The offer of a contact over a conversation that exists: what we call them and what they may do here. */
export function OfferContact({ via, name, toast, onDone }: { via: string; name: string; toast: (text: string) => void; onDone?: () => void }) {
  const [open, setOpen] = useState(false);
  const [label, setLabel] = useState(name);
  const [level, setLevel] = useState<ContactLevel>("read");
  const [busy, setBusy] = useState(false);
  if (!open) return <button type="button" className="btn small" data-contact-offer="" onClick={() => setOpen(true)}><Icon name="plus" size={13} />{t("contact.offer")}</button>;
  async function send() {
    if (busy) return;
    setBusy(true);
    try {
      await api.post(`${CONTACTS}/offer`, { via, name: label.trim(), tools: level });
      invalidate(CONTACTS);
      setOpen(false);
      onDone?.();
      toast(t("contact.offered"));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="contact-form" data-contact-offer-form="">
      <div className="sub">{t("contact.offer.hint")}</div>
      <input className="field" value={label} maxLength={80} aria-label={t("contact.name")} placeholder={t("contact.name")} onChange={(e) => setLabel(e.target.value)} />
      <span className="sub contact-caption">{t("contact.level")}</span>
      <LevelPick value={level} onChange={setLevel} />
      <div className="contact-actions">
        <button type="button" className="btn small primary" data-contact-send-offer="" disabled={busy} onClick={() => void send()}>{t("contact.offer.send")}</button>
        <button type="button" className="btn small ghost" disabled={busy} onClick={() => setOpen(false)}>{t("common.cancel")}</button>
      </div>
    </div>
  );
}

function LevelPick({ value, onChange }: { value: ContactLevel; onChange: (level: ContactLevel) => void }) {
  return <Segmented<ContactLevel> value={value} label={t("contact.level")} onChange={onChange} options={LEVELS.map((id) => ({ id, label: t(`contact.level.${id}`) }))} />;
}

/** Every contact of the installation, whichever chat it belongs to, with this side's own fingerprint to compare. */
export function ContactsSection({ sessionId, toast }: { sessionId: string; toast: (text: string) => void }) {
  const { data } = useQuery<ContactList>(CONTACTS, { pollMs: POLL_MS });
  const contacts = data?.contacts ?? [];
  if (!data || (!contacts.length)) return null;
  return (
    <section className="subagents-group contacts" data-group="contacts" data-contacts="">
      <div className="dt-label"><span>{t("contact.group")}</span><span className="dt-aside">{contacts.filter((c) => c.state !== "broken").length}</span></div>
      {data.fingerprint && <div className="sub contacts-mine" data-contacts-mine="">{t("contact.mine", { fp: data.fingerprint })}</div>}
      <BackgroundNote />
      {contacts.map((contact) => <ContactRow key={contact.id} contact={contact} sessionId={sessionId} toast={toast} />)}
    </section>
  );
}

function ContactRow({ contact, sessionId, toast }: { contact: Contact; sessionId: string; toast: (text: string) => void }) {
  const [busy, setBusy] = useState(false);
  const [writing, setWriting] = useState(false);
  const state = contactState(contact);
  const live = contact.state === "active" || contact.state === "accepting" || contact.state === "offered";
  async function act(work: () => Promise<unknown>) {
    if (busy) return;
    setBusy(true);
    try {
      await work();
      invalidate(CONTACTS);
      invalidate(`/api/sessions/${encoded(sessionId)}/agent-talks`);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  const path = `${CONTACTS}/${encoded(contact.id)}`;
  const leave = async () => {
    if (await confirmAsync(t("contact.break.ask", { name: contact.name }), { action: t("contact.break"), danger: true })) void act(() => api.post(`${path}/break`));
  };
  const meta = [
    contact.level && contact.state !== "incoming" ? t(`contact.level.${contact.level}`) : "",
    contact.state !== "incoming" ? usd(contact.spent_usd) : "",
    contact.last_at ? relTime(contact.last_at) : "",
    contact.session_id === sessionId ? t("contact.here") : "",
  ].filter(Boolean).join(" · ");
  return (
    <article className="contact" data-contact={contact.id} data-contact-state={state.key}>
      <div className="contact-head">
        <Icon name="user" size={14} />
        <span className="contact-name truncate">{contact.name}</span>
        <span className={`chip ${state.tone}`} data-contact-chip="">{t(`contact.state.${state.key}`)}</span>
      </div>
      <KeyLine fingerprint={contact.fingerprint} />
      {meta && <div className="sub contact-meta">{meta}</div>}
      {contact.state === "incoming" ? (
        <Incoming contact={contact} sessionId={sessionId} busy={busy} act={act} />
      ) : live ? (
        <>
          {contact.state === "active" && contact.level !== "stand_in" && contact.level && (
            <LevelPick value={contact.level as ContactLevel} onChange={(tools) => void act(() => api.put(path, { tools }))} />
          )}
          {writing && contact.talk_id && <OwnerSay path={`${path}/say`} toast={toast} onSent={() => { setWriting(false); invalidate(CONTACTS); }} />}
          <div className="contact-actions">
            {contact.state === "active" && contact.talk_id && !writing && <button type="button" className="btn small" data-contact-write="" onClick={() => setWriting(true)}>{t("owner.say")}</button>}
            <button type="button" className="btn small danger" data-contact-break="" disabled={busy} onClick={() => void leave()}>{t("contact.break")}</button>
          </div>
        </>
      ) : null}
    </article>
  );
}

/** An offer that waits: the other side's words as they wrote them, and where the contact will live here. */
function Incoming({ contact, sessionId, busy, act }: { contact: Contact; sessionId: string; busy: boolean; act: (work: () => Promise<unknown>) => void }) {
  const [where, setWhere] = useState<"here" | "new">("here");
  const [level, setLevel] = useState<ContactLevel>("read");
  const path = `${CONTACTS}/${encoded(contact.id)}`;
  return (
    <div className="contact-incoming" data-contact-incoming="">
      {contact.offer_text && <blockquote className="contact-quote"><div className="talk-text">{contact.offer_text}</div></blockquote>}
      <div className="sub">{t("contact.compare")}</div>
      <span className="sub contact-caption">{t("contact.where")}</span>
      <Segmented<"here" | "new"> value={where} label={t("contact.where")} onChange={setWhere} options={[{ id: "here", label: t("contact.where.here") }, { id: "new", label: t("contact.where.new") }]} />
      <span className="sub contact-caption">{t("contact.level")}</span>
      <LevelPick value={level} onChange={setLevel} />
      <div className="contact-actions">
        <button type="button" className="btn small primary" data-contact-accept="" disabled={busy}
          onClick={() => act(() => api.post(`${path}/accept`, { session_id: where === "here" ? sessionId : "", tools: level }))}>{t("contact.accept")}</button>
        <button type="button" className="btn small" data-contact-decline="" disabled={busy} onClick={() => act(() => api.post(`${path}/decline`))}>{t("contact.decline")}</button>
      </div>
    </div>
  );
}
