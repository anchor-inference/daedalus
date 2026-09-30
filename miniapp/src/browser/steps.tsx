// The recording of the operator's own steps, in the Browser tab: the button that starts it while they
// drive, the bar that shows each step as it is taken, and, once it stops, the card that turns it into a
// skill draft for them to read, edit and approve. Nothing of it reaches an agent until they approve.
//
// The recording is theirs alone and never silent: it starts only from this button, says so on the
// picture while it runs, and ends with the Stop button or with the browser given back.

import { useEffect, useMemo, useRef, useState } from "react";
import type { BrowserDraft, BrowserSiteNote, BrowserStep, BrowserWorkflow } from "../api";
import { Popover } from "../dialogs";
import { plural, t } from "../i18n";
import { Icon } from "../icons";
import { confirmAsync, errorText, haptic } from "../ui";
import { approveNote, deleteNote, discardWorkflow, draftWorkflow, editNote, markWorkflow, startWorkflow, stopWorkflow } from "./data";
import type { LiveWorkflow } from "./live";
import { doingSteps, stepWords } from "./model";

/** What the panel knows of the recording now: the socket's word when it has one, else the host's. */
export type Recording = { id: string; recording: boolean; steps: BrowserStep[]; count: number };

export function currentRecording(live: LiveWorkflow | null, listed: BrowserWorkflow | null): Recording | null {
  if (live && (!listed || live.id === listed.id || !live.recording)) {
    // A view that attached midway heard only the steps since; the host's listing has the earlier ones.
    const earlier = listed && listed.id === live.id ? listed.steps.filter((s) => !live.steps.some((x) => x.n === s.n)) : [];
    const steps = [...earlier, ...live.steps].sort((a, b) => a.n - b.n);
    if (live.recording) return { id: live.id, recording: true, steps, count: Math.max(live.count, steps.length) };
    // Stopped on the socket: the listing read before it stopped is behind, whatever it says.
    if (listed && listed.id !== live.id && listed.state === "recording") return { id: listed.id, recording: true, steps: listed.steps, count: listed.steps.length };
    return null;
  }
  if (listed && listed.state === "recording") return { id: listed.id, recording: true, steps: listed.steps, count: listed.steps.length };
  return null;
}

export function StepText({ step }: { step: BrowserStep }) {
  const words = stepWords(step);
  return <>{t(words.key, words.vars)}</>;
}

/** The toolbar's button: start recording, with the one choice there is (whether typed values are kept). */
export function RecordButton({ group, toast }: { group: string; toast: (text: string) => void }) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null);
  const [keep, setKeep] = useState(false);
  const [busy, setBusy] = useState(false);
  const start = async () => {
    setBusy(true);
    try {
      await startWorkflow(group, keep ? "literal" : "slots");
      haptic("medium");
      toast(t("browser.steps.started"));
      setAnchor(null);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button type="button" className="btn small bp-rec-btn" onClick={(e) => setAnchor(e.currentTarget)} aria-haspopup="dialog" title={t("browser.steps.record.title")}>
        <span className="bp-rec-dot" aria-hidden="true" />
        {t("browser.steps.record")}
      </button>
      {anchor && (
        <Popover anchor={anchor} onClose={() => setAnchor(null)} align="right" className="bp-rec-start" label={t("browser.steps.start.head")}>
          <div className="bp-rec-start-head">{t("browser.steps.start.head")}</div>
          <p>{t("browser.steps.start.body")}</p>
          <p className="bp-rec-start-secret"><Icon name="lock" size={13} /> <span>{t("browser.steps.start.secret")}</span></p>
          <label className="bp-rec-start-keep">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
            <span>
              <b>{t("browser.steps.start.values")}</b>
              <span className="sub">{t("browser.steps.start.values.sub")}</span>
            </span>
          </label>
          <div className="bp-rec-start-foot">
            <button type="button" className="btn small primary" disabled={busy} onClick={() => void start()}>
              <span className="bp-rec-dot" aria-hidden="true" />
              {t("browser.steps.start.go")}
            </button>
          </div>
        </Popover>
      )}
    </>
  );
}

/** The strip over the picture while the operator's steps are recorded: that it is on, how many, the
 *  latest in words, and the two things they can do about it. */
export function RecordBar({ group, recording, tab, toast }: { group: string; recording: Recording; tab: string | null; toast: (text: string) => void }) {
  const [busy, setBusy] = useState(false);
  const latest = recording.steps[recording.steps.length - 1] ?? null;
  const run = async (what: () => Promise<void>) => {
    setBusy(true);
    try {
      await what();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="bp-recbar" role="status" data-recording={recording.id}>
      <span className="bp-rec-dot live" aria-hidden="true" />
      <b className="bp-recbar-title">{t("browser.steps.bar")}</b>
      <span className="bp-recbar-count">{plural("browser.steps.count", recording.count)}</span>
      <span className="bp-recbar-latest truncate">{latest ? <StepText step={latest} /> : null}</span>
      {/* The press must not take the page's selection away: mousedown would move the focus first. */}
      <button type="button" className="btn small bp-recbar-mark" disabled={busy} title={t("browser.steps.mark.title")} onMouseDown={(e) => e.preventDefault()}
        onClick={() => void run(async () => {
          const step = await markWorkflow(group, tab);
          toast(t("browser.steps.marked", { text: step.text || step.title || "" }));
        })}>
        <Icon name="check" size={13} />
        {t("browser.steps.mark")}
      </button>
      <button type="button" className="btn small bp-recbar-stop" disabled={busy} title={t("browser.steps.stop.title")} onClick={() => void run(async () => { await stopWorkflow(group); })}>
        <Icon name="stop" size={12} />
        {t("browser.steps.stop")}
      </button>
    </div>
  );
}

/** A finished recording, under the picture: its steps, the operator's goal for it, and the button
 *  that drafts it. The panel then shows the draft in its place (`onDraft`), and the recording, drafted,
 *  leaves this card. */
export function RecordedCard({ group, recording, toast, onDraft }: { group: string; recording: BrowserWorkflow; toast: (text: string) => void; onDraft: (draft: BrowserDraft) => void }) {
  const [open, setOpen] = useState(false);
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const doing = doingSteps(recording.steps);
  const make = async () => {
    setBusy(true);
    try {
      onDraft(await draftWorkflow(group, recording.id, goal.trim()));
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const discard = async () => {
    if (!(await confirmAsync(t("browser.steps.discard.title"), { body: t("browser.steps.discard.body"), action: t("browser.steps.discard"), danger: true }))) return;
    try {
      await discardWorkflow(group, recording.id);
    } catch (e) {
      toast(errorText(e));
    }
  };
  return (
    <section className="bp-rec" data-workflow={recording.id} data-state="stopped" aria-label={plural("browser.steps.done", doing)}>
      <div className="bp-rec-head">
        <Icon name="journal" size={14} />
        <b>{plural("browser.steps.done", doing)}</b>
        <button type="button" className="bp-rec-toggle" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
          {t(open ? "browser.steps.hide" : "browser.steps.show")}
          <span className={`chev ${open ? "open" : ""}`}><Icon name="chevron" size={12} /></span>
        </button>
      </div>
      {open && <StepList steps={recording.steps} />}
      <form className="bp-rec-foot" onSubmit={(e) => { e.preventDefault(); void make(); }}>
        <input className="bp-rec-goal" value={goal} maxLength={300} onChange={(e) => setGoal(e.target.value)} aria-label={t("browser.steps.goal")} placeholder={t("browser.steps.goal.placeholder")} />
        <button type="submit" className="btn small primary" disabled={busy}>{t(busy ? "browser.steps.drafting" : "browser.steps.draft")}</button>
        <button type="button" className="btn small" disabled={busy} onClick={() => void discard()}>{t("browser.steps.discard")}</button>
      </form>
    </section>
  );
}

/** The newest finished recording still waiting for the operator's decision: not drafted, and with a
 *  step that does something (one that only watched the page scroll is not worth a card). */
export function waitingRecording(recent: BrowserWorkflow[]): BrowserWorkflow | null {
  return recent.find((w) => w.state === "stopped" && !w.note_id && doingSteps(w.steps) > 0) ?? null;
}

export function StepList({ steps }: { steps: BrowserStep[] }) {
  return (
    <ol className="bp-rec-steps">
      {steps.map((s) => (
        <li key={s.n} data-action={s.action} className={s.action === "handoff" ? "yours" : s.action === "arrive" || s.action === "scroll" ? "quiet" : ""}>
          {s.action === "handoff" && <Icon name="lock" size={12} />}
          <span className="truncate"><StepText step={s} /></span>
        </li>
      ))}
    </ol>
  );
}

/** The draft as the operator reads it: its title and steps to edit, then approve, keep for later or
 *  discard. Approving saves their words first: what agents read is what they approved. */
export function ProcedureEditor({ draft, toast, onDone }: { draft: BrowserDraft | { note: BrowserSiteNote; drafted_by?: undefined; why?: undefined }; toast: (text: string) => void; onDone: () => void }) {
  const note = draft.note;
  const [title, setTitle] = useState(note.title);
  const [text, setText] = useState(note.text);
  const [busy, setBusy] = useState(false);
  const field = useRef<HTMLTextAreaElement>(null);
  useEffect(() => field.current?.focus({ preventScroll: true }), []);
  const changed = title !== note.title || text !== note.text;
  const run = async (what: () => Promise<void>) => {
    setBusy(true);
    try {
      await what();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  };
  const by = useMemo(() => (draft.drafted_by === "model" ? t("browser.steps.draft.by.model") : draft.drafted_by === "steps" ? t("browser.steps.draft.by.steps") : ""), [draft.drafted_by]);
  return (
    <div className="bp-proc" data-note={note.id}>
      <div className="bp-rec-head">
        <Icon name="skill" size={14} />
        <b>{t("browser.steps.draft.head")}</b>
        <span className="bp-proc-host mono">{note.host}</span>
      </div>
      {by && <div className="sub bp-proc-by">{by}{draft.why ? ` ${t("browser.steps.draft.why", { why: draft.why })}` : ""}</div>}
      <label className="bp-proc-label" htmlFor={`bp-proc-title-${note.id}`}>{t("browser.steps.draft.title")}</label>
      <input id={`bp-proc-title-${note.id}`} className="bp-proc-title" value={title} maxLength={120} onChange={(e) => setTitle(e.target.value)} />
      <label className="bp-proc-label" htmlFor={`bp-proc-text-${note.id}`}>{t("browser.steps.draft.text")}</label>
      <textarea id={`bp-proc-text-${note.id}`} ref={field} className="bp-proc-text" rows={10} value={text} maxLength={4000} onChange={(e) => setText(e.target.value)} />
      {note.status === "active" ? (
        <div className="bp-proc-foot">
          <button type="button" className="btn small primary" disabled={busy || !changed || !text.trim() || !title.trim()} onClick={() => void run(async () => {
            await editNote(note.id, text, title);
            toast(t("browser.steps.saved"));
            onDone();
          })}>{t("browser.steps.save")}</button>
          <button type="button" className="btn small" disabled={busy} onClick={onDone}>{t("bs.notes.cancel")}</button>
        </div>
      ) : (
      <div className="bp-proc-foot">
        <button type="button" className="btn small primary" disabled={busy || !text.trim() || !title.trim()} onClick={() => void run(async () => {
          if (changed) await editNote(note.id, text, title);
          await approveNote(note.id);
          toast(t("browser.steps.approved", { host: note.host }));
          onDone();
        })}>{t("browser.steps.approve")}</button>
        <button type="button" className="btn small" disabled={busy || !changed || !text.trim() || !title.trim()} onClick={() => void run(async () => {
          await editNote(note.id, text, title);
          toast(t("browser.steps.saved"));
        })}>{t("browser.steps.save")}</button>
        <button type="button" className="btn small" disabled={busy} title={t("browser.steps.later.title")} onClick={() => void run(async () => {
          if (changed) await editNote(note.id, text, title);
          onDone();
        })}>{t("browser.steps.later")}</button>
        <button type="button" className="btn small danger" disabled={busy} onClick={() => void run(async () => {
          await deleteNote(note.id);
          toast(t("browser.steps.discarded"));
          onDone();
        })}>{t("browser.steps.discard")}</button>
      </div>
      )}
    </div>
  );
}
