// Meeting notes in the composer: an audio file attached, or a recording the operator turned into a
// meeting, becomes a transcript, then a summary, decisions and action items written by the session's
// model. The card above the field says how far along it is, and when the notes are in, offers the
// action items as one calendar entry on a chosen day and the notes as a message in the chat.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "./api";
import { errorText } from "./ui";
import { Icon } from "./icons";
import { t } from "./i18n";

export type MeetingNotes = { title: string; summary: string; decisions: string[]; actions: string[] };
export type MeetingJob = {
  id: string; status: "queued" | "transcribing" | "writing" | "done" | "failed"; name: string;
  done: number; total: number; seconds: number; notes: MeetingNotes | Record<string, never>;
  file: string; kept: string; error: string; calendar_event: string;
};

type MeetingState =
  | { phase: "uploading"; name: string; fraction: number }
  | { phase: "job"; job: MeetingJob }
  | { phase: "failed"; name: string; error: string };

const AUDIO_NAME = /\.(mp3|m4a|wav|ogg|oga|opus|webm|flac|aac|amr|wma)$/i;

/** An attachment the composer offers to make meeting notes from. */
export function isAudio(file: File): boolean {
  return file.type.startsWith("audio/") || (!file.type && AUDIO_NAME.test(file.name));
}

/** Today in the reader's own calendar, as the date field writes it. */
function today(): string {
  const now = new Date();
  return `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, "0")}-${String(now.getDate()).padStart(2, "0")}`;
}

export function useMeeting(sessionId: string, toast: (text: string) => void) {
  const [state, setState] = useState<MeetingState | null>(null);
  const session = useRef(sessionId);
  session.current = sessionId;
  // Another session starts clean; the job goes on on the host and its notes land in that session's inbox.
  useEffect(() => setState(null), [sessionId]);

  const start = useCallback(async (file: File, keep: boolean) => {
    const owner = session.current;
    setState({ phase: "uploading", name: file.name, fraction: 0 });
    const form = new FormData();
    form.append("audio", file, file.name);
    form.append("keep", keep ? "true" : "false");
    try {
      const job = await api.upload<MeetingJob>(`/api/sessions/${owner}/meeting`, form, (fraction) => {
        if (session.current === owner) setState({ phase: "uploading", name: file.name, fraction });
      });
      if (session.current === owner) setState({ phase: "job", job });
    } catch (e) {
      if (session.current === owner) setState({ phase: "failed", name: file.name, error: errorText(e) });
    }
  }, []);

  const jobId = state?.phase === "job" && !["done", "failed"].includes(state.job.status) ? state.job.id : "";
  useEffect(() => {
    if (!jobId) return;
    let alive = true;
    const timer = window.setInterval(async () => {
      try {
        const job = await api.get<MeetingJob>(`/api/sessions/${session.current}/meeting/${jobId}`);
        if (alive) setState((prev) => (prev?.phase === "job" && prev.job.id === job.id ? { phase: "job", job } : prev));
      } catch (e) {
        if (alive) setState((prev) => (prev?.phase === "job" ? { phase: "failed", name: prev.job.name, error: errorText(e) } : prev));
      }
    }, 1500);
    return () => { alive = false; window.clearInterval(timer); };
  }, [jobId]);

  const toCalendar = useCallback(async (day: string) => {
    if (state?.phase !== "job") return;
    try {
      await api.post(`/api/sessions/${session.current}/meeting/${state.job.id}/calendar`, { day });
      setState({ phase: "job", job: { ...state.job, calendar_event: "added" } });
      toast(t("meeting.calendar.added", { day }));
    } catch (e) {
      toast(errorText(e));
    }
  }, [state, toast]);

  const send = useCallback(async () => {
    if (state?.phase !== "job") return;
    try {
      await api.post(`/api/sessions/${session.current}/meeting/${state.job.id}/send`);
      setState(null);
      toast(t("meeting.sent"));
    } catch (e) {
      toast(errorText(e));
    }
  }, [state, toast]);

  return { state, start, toCalendar, send, close: () => setState(null) };
}

export type Meeting = ReturnType<typeof useMeeting>;

/** Above the field while an audio file is attached: the offer, and whether to keep the recording. */
export function MeetingOffer({ file, onStart }: { file: File; onStart: (keep: boolean) => void }) {
  const [keep, setKeep] = useState(false);
  return (
    <div className="meeting-card meeting-offer" role="group" aria-label={t("meeting.offer")}>
      <Icon name="mic" size={16} />
      <span className="meeting-text">
        <span className="meeting-title">{t("meeting.offer")}</span>
        <span className="sub truncate">{t("meeting.offer.sub", { name:file.name })}</span>
      </span>
      <label className="meeting-keep sub"><input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />{t("meeting.keep")}</label>
      <button type="button" className="btn small primary" onClick={() => onStart(keep)}>{t("meeting.start")}</button>
    </div>
  );
}

function List({ title, items }: { title: string; items: string[] }) {
  if (!items.length) return null;
  return <div className="meeting-list"><div className="meeting-label">{title}</div><ul>{items.map((item, i) => <li key={i}>{item}</li>)}</ul></div>;
}

/** The job's progress, then its notes with the calendar and the chat. */
export function MeetingCard({ meeting }: { meeting: Meeting }) {
  const [day, setDay] = useState(today);
  const state = meeting.state;
  if (!state) return null;
  const close = <button type="button" className="iconbtn flat small" onClick={meeting.close} aria-label={t("meeting.close")} title={t("meeting.close")}><Icon name="close" size={15} /></button>;
  if (state.phase === "uploading" || (state.phase === "job" && !["done", "failed"].includes(state.job.status))) {
    const line = state.phase === "uploading"
      ? t("meeting.progress.upload", { percent:Math.floor(state.fraction * 100) })
      : state.job.status === "writing"
        ? t("meeting.progress.notes")
        : state.job.total
          ? t("meeting.progress.transcribe", { done:state.job.done, total:state.job.total })
          : t("meeting.progress.start");
    const fraction = state.phase === "uploading" ? state.fraction : state.job.status === "writing" ? 1 : state.job.total ? state.job.done / state.job.total : 0;
    return (
      <div className="meeting-card meeting-progress" role="status" data-phase={state.phase === "uploading" ? "uploading" : state.job.status}>
        <span className="voicebar-spinner" aria-hidden />
        <span className="meeting-text">
          <span className="meeting-title">{t("meeting.offer")}</span>
          <span className="sub">{line}</span>
          <span className="meeting-bar" style={{ ["--v" as string]: Math.round(fraction * 100) }}><i /></span>
        </span>
      </div>
    );
  }
  if (state.phase === "failed" || (state.phase === "job" && state.job.status === "failed")) {
    const error = state.phase === "failed" ? state.error : state.job.error;
    return (
      <div className="meeting-card meeting-failed" role="alert">
        <Icon name="alert" size={16} />
        <span className="meeting-text"><span className="meeting-title">{t("meeting.failed")}</span><span className="sub">{error}</span></span>
        {close}
      </div>
    );
  }
  const job = state.job;
  const notes = job.notes as MeetingNotes;
  return (
    <div className="meeting-card meeting-done" role="region" aria-label={t("meeting.offer")}>
      <div className="meeting-head">
        <span className="meeting-title grow">{notes.title || t("meeting.offer")}</span>
        {close}
      </div>
      {notes.summary && <p className="meeting-summary">{notes.summary}</p>}
      <List title={t("meeting.decisions")} items={notes.decisions ?? []} />
      <List title={t("meeting.actions")} items={notes.actions ?? []} />
      <div className="sub meeting-file">{t("meeting.file", { file:job.file })}{job.kept ? ` · ${t("meeting.kept", { file:job.kept })}` : ""}</div>
      <div className="meeting-actions">
        {(notes.actions ?? []).length > 0 && (job.calendar_event
          ? <span className="sub meeting-added"><Icon name="check" size={14} />{t("meeting.calendar.done")}</span>
          : <>
              <input type="date" className="field small" value={day} onChange={(e) => setDay(e.target.value)} aria-label={t("meeting.calendar.day")} />
              <button type="button" className="btn small" data-action="calendar" onClick={() => void meeting.toCalendar(day)}>{t("meeting.calendar.add")}</button>
            </>)}
        <button type="button" className="btn small primary" data-action="send" onClick={() => void meeting.send()}>{t("meeting.send")}</button>
      </div>
    </div>
  );
}
