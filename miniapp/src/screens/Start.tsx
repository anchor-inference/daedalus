// The screen you land on: a greeting and the composer, so a chat begins by typing it.
// Naming it, choosing a folder and a loop stay in the new-agent sheet for when they matter.
// On a phone it is the home of the redesign: the composer at the bottom, the model in the title and
// only the live chats above it; the full list is the Chats page (Chats.tsx), at ?view=chats.

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { api, AsrStatus, Preset, Project, SessionList, SessionSummary, Settings } from "../api";
import { fieldHeight } from "../composer";
import { AttachmentCard, PlusSheet, VoiceCircle, useComposerFocus } from "../composerbox";
import { Popover } from "../ui/dialogs";
import { effortBody, effortOf, presetEffort, type Effort } from "../starteffort";
import { ModelChoice, ModelSelect } from "../modelselect";
import { MicButton, VoiceBar, VoiceNoteFailed, useVoiceNote } from "../voicebar";
import { landWords } from "../voicenote";
import { NewAgentSheet } from "./Sessions";
import { ChatsScreen } from "./Chats";
import { CHATS_PATH } from "../drawer";
import { Icon } from "../icons";
import { navigate, pathFor, sessionPath, useRoute } from "../router";
import { invalidate, useOffline, useQuery } from "../store";
import { useStreamUp } from "../events";
import { useSummary } from "../notifications";
import { agentsListingOf } from "../mode";
import { agentName, kindOf } from "../grouping";
import { relTime, shortModel, untilShort } from "../format";
import { fmtInterval, statusWord } from "../ui/components";
import { screenTitle, useMedia } from "../ui/index";
import { Banner, IconButton, ListRow, SectionHeader, SheetRow, TopBar } from "../ui/phone";
import { enterSends, errorText } from "../ui";
import { t } from "../i18n";
import { EnvPill } from "../envpill";
import { RunOn, RunOnRow, RunOnSelect, useRunOn } from "../runon";

export function StartScreen({ onOpen, toast, project = "", projects = [], onProjects, onPickProject }: { onOpen: (id: string) => void; toast: (t: string) => void; project?: string; projects?: Project[]; onProjects?: () => void; onPickProject?: (id: string) => void }) {
  const phone = !useMedia("(min-width: 1024px)");
  const route = useRoute();
  const creating = route.query.get("new") === "1";
  const closeNew = () => navigate(pathFor("agents"), { replace: true });
  const model = useStartModel();
  const run = useRunOn(project);
  const sheet = creating && <NewAgentSheet onClose={closeNew} onCreated={onOpen} toast={toast} project={project} />;
  if (phone && route.query.get("view") === "chats") {
    return <>
      <ChatsScreen onOpen={onOpen} toast={toast} project={project} projects={projects} onPickProject={onPickProject} />
      {sheet}
    </>;
  }
  if (phone) {
    return <>
      <PhoneHome onOpen={onOpen} toast={toast} project={project} projects={projects} model={model} run={run} onProjects={onProjects} />
      {sheet}
    </>;
  }
  return (
    <>
      <div className="start">
        <div className="start-hero">
          <h1 className="start-greeting">{t("start.greeting")}</h1>
          <StartComposer phone={false} project={project} toast={toast} model={model} run={run} />
        </div>
      </div>
      {sheet}
    </>
  );
}

/** How many live chats the home shows above its composer; the rest are one tap away in Chats. */
const LIVE = 3;

/**
 * A phone's home is a new chat: the composer at the bottom, the model in the title, and above it only
 * what is live — the chats that need the operator, the ones at work, the loops — three at most. The
 * whole list is the Chats page (the drawer, or "See all"). Offline, the live rows stay, dimmed, with
 * the time they were last confirmed, and the composer keeps the draft until the bot is back.
 */
function PhoneHome({ onOpen, toast, project, projects, model, run, onProjects }: { onOpen: (id: string) => void; toast: (t: string) => void; project: string; projects: Project[]; model: StartModel; run: RunOn; onProjects?: () => void }) {
  const live = useStreamUp();
  const offline = useOffline();
  const summary = useSummary();
  const { data, updatedAt } = useQuery<SessionList>("/api/sessions?view=all", { pollMs: live ? 60000 : 5000, staleMs: 3000 });
  const lens = projects.find((p) => p.id === project);
  const rows = useMemo(() => {
    const listing = agentsListingOf(data);
    const rank = (s: SessionSummary) => ({ waiting: 0, working: 1, loop: 2, idle: 3 })[kindOf(s, false)];
    return (listing?.sessions ?? [])
      .filter((s) => !s.metadata?.subagent_of && !s.archived && (!project || s.project_id === project) && rank(s) < 3)
      .sort((a, b) => rank(a) - rank(b) || Date.parse(b.last_message_at) - Date.parse(a.last_message_at));
  }, [data, project]);
  const waiting = rows.filter((s) => s.status === "waiting").length;
  const shown = rows.slice(0, LIVE);
  const confirmed = updatedAt ? new Date(updatedAt).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
  return (
    <div className="ph-page ph-home">
      <TopBar
        center
        pip={waiting > 0}
        title={<>{model.label}<Icon name="chevron" size={14} /></>}
        onTitle={() => model.setOpen(true)}
        titleLabel={`${t("session.model.for")}: ${model.label}`}
        actions={<IconButton icon="bell" label={screenTitle("inbox")} href={pathFor("inbox")} badge={summary.unseen} />}
      />
      {offline && <Banner strip tone="bad" icon="offline">{t("app.offline")}</Banner>}
      <div className="ph-page-body">
        <div className={`ph-hero ${offline ? "offline" : ""}`}>
          <Icon name="logo" size={36} />
          <h1>{t("start.greeting")}</h1>
          {lens && onProjects && <button type="button" className="ph-btn sm" onClick={onProjects}><Icon name="folder" size={16} />{lens.name}</button>}
          {/* The host is picked in the + sheet; said here too, since the composer at rest has no room for it. */}
          {run.env === "host" && <span className="runon-hero" aria-label={`${t("runon.label")}: ${t("term.env.host")}`}><EnvPill env="host" /></span>}
        </div>
        {shown.length > 0 && (
          <>
            <SectionHeader count={offline && confirmed ? t("ph.live.confirmed", { time: confirmed }) : undefined}
              action={<button type="button" className="ph-link" onClick={() => navigate(CHATS_PATH)}>{t("ph.seeall")}</button>}>{t("ph.live")}</SectionHeader>
            <div className={`ph-list ph-live ${offline ? "stale" : ""}`}>
              {shown.map((s) => (
                <ListRow key={s.id} data={{ session: s.id }} title={agentName(s)} onOpen={() => onOpen(s.id)}
                  lead={<span className="ph-sicon"><Icon name={s.metadata?.loop ? "loop" : "bots"} size={18} />{s.status !== "idle" && <span className={`ph-st ${s.status}`} />}</span>}
                  meta={<LiveMeta s={s} />}
                  trail={relTime(s.last_message_at)} />
              ))}
            </div>
          </>
        )}
      </div>
      <StartComposer phone project={project} toast={toast} model={model} run={run} />
    </div>
  );
}

function LiveMeta({ s }: { s: SessionSummary }) {
  if (s.status === "waiting" || s.status === "running" || s.status === "failed") {
    return <><span className={s.status}>{statusWord(s.status)}</span>{s.model && <><span className="ph-sep" /><span className="ph-ell">{shortModel(s.model, 28)}</span></>}</>;
  }
  const loop = s.metadata?.loop;
  const words = [t("ph.loop"), loop?.mode === "interval" ? t("loop.every", { t: fmtInterval(loop.interval_seconds) }) : t("loop.selfpaced"), loop?.next_run_at ? t("ph.loop.next", { t: untilShort(loop.next_run_at) }) : ""].filter(Boolean).join(" · ");
  return <span className="ph-ell">{words}</span>;
}

type Chosen = { preset: string } | { provider: string; model: string } | { model: string };

/** The model and effort of the chat about to start: held above the composer because a phone picks
 *  the model from the page's title, a desktop from the composer's own control, and both open one list. */
type StartModel = ReturnType<typeof useStartModel>;

function useStartModel() {
  const settings = useQuery<Settings>("/api/settings", { staleMs: 60000 });
  const presets = settings.data?.presets ?? {};
  const defaultId = settings.data?.model?.preset ?? "";
  const [chosen, setChosen] = useState<Chosen | null>(null);
  const [open, setOpen] = useState(false);
  // Picked here and carried into the session it makes; null leaves the model's own.
  const [effort, setEffort] = useState<Effort | null>(null);
  const labelOf = (id: string) => {
    const item = presets[id] as Preset | undefined;
    return item ? item.label || `${item.provider}/${item.model}` : id;
  };
  const label = chosen
    ? "preset" in chosen ? labelOf(chosen.preset) : "provider" in chosen ? `${chosen.provider}/${chosen.model}` : chosen.model
    : defaultId ? labelOf(defaultId) : t("newagent.model.default");
  // A model picked outside the presets keeps the thinking the session starts with, the default's.
  const presetId = chosen && "preset" in chosen ? chosen.preset : defaultId;
  const shownEffort = effort ?? presetEffort(presets[presetId] as Preset | undefined);
  function choose(choice: ModelChoice) {
    if ("clear" in choice) setChosen(null);
    else if ("preset" in choice) setChosen({ preset: choice.preset });
    else if ("provider" in choice) setChosen({ provider: choice.provider, model: choice.model });
    else setChosen({ model: choice.model });
  }
  return { chosen, label, effort, setEffort, shownEffort, choose, open, setOpen };
}

function StartComposer({ phone, project, toast, model, run }: { phone: boolean; project: string; toast: (t: string) => void; model: StartModel; run: RunOn }) {
  const { chosen, effort, setEffort, shownEffort, choose } = model;
  const modelLabel = model.label;
  const modelOpen = model.open;
  const setModelOpen = model.setOpen;
  const [draft, setDraft] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [busy, setBusy] = useState(false);
  const [progress, setProgress] = useState<number | null>(null);
  const focus = useComposerFocus(phone);
  const focused = focus.focused;
  const offline = useOffline();
  const { data: asr } = useQuery<AsrStatus>("/api/asr", { staleMs: 60000 });
  const field = useRef<HTMLTextAreaElement>(null);
  const fileInput = useRef<HTMLInputElement>(null);
  const photoInput = useRef<HTMLInputElement>(null);
  const plusButton = useRef<HTMLButtonElement>(null);
  const [plusOpen, setPlusOpen] = useState(false);

  // A desktop lands here to type. A phone keeps the keyboard down until the field is touched:
  // opening it over the list of chats hides the thing the reader came to find.
  useEffect(() => {
    if (!phone) field.current?.focus();
  }, [phone]);
  const fit = () => {
    const el = field.current;
    if (!el) return;
    el.style.height = "auto";
    const cs = getComputedStyle(el);
    const line = parseFloat(cs.lineHeight);
    const pad = parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom);
    el.style.height = `${fieldHeight(el.scrollHeight, line, pad, 1, phone ? 5 : undefined)}px`;
  };
  useLayoutEffect(fit, [draft, phone]);

  async function send(words?: string) {
    const text = (words ?? draft).trim();
    if (busy || (!text && files.length === 0)) return;
    setBusy(true);
    let created: { id: string } | null = null;
    let sent = false;
    try {
      // The message goes out after the session exists, so a model picked here is the one that
      // reads it. Sending both in the create call would start the run on the default model.
      created = await api.post<{ id: string }>("/api/sessions", {
        autotitle: true,
        title: text || files[0]?.name,
        preset: chosen && "preset" in chosen ? chosen.preset : undefined,
        project_id: project || undefined,
        ...run.body,
      });
      // The model and the effort in one change, before the message: the first turn is the one
      // that has to think the way the operator asked.
      const model = chosen && !("preset" in chosen) ? ("provider" in chosen ? { provider: chosen.provider, model: chosen.model } : { model: chosen.model }) : null;
      if (model || effort) {
        await api.post(`/api/sessions/${created.id}/model`, { ...model, ...(effort ? effortBody(effort) : {}) });
      }
      if (files.length) {
        const form = new FormData();
        if (text) form.append("text", text);
        for (const file of files) form.append("files", file);
        setProgress(0);
        await api.upload(`/api/sessions/${created.id}/upload`, form, setProgress);
      } else {
        await api.post(`/api/sessions/${created.id}/messages`, { text });
      }
      sent = true;
      setDraft("");
      setFiles([]);
      setEffort(null);
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
      setProgress(null);
    }
    if (created && sent) {
      invalidate("/api/sessions");
      navigate(sessionPath(created.id));
    } else if (created) {
      // A 150 MB video refused mid-upload used to leave the screen for the new, empty session: the
      // words and the file were gone and the list held a conversation with nothing in it. The draft
      // stays here to send again, and the empty session goes.
      void api.delete(`/api/sessions/${created.id}`).catch(() => undefined).finally(() => invalidate("/api/sessions"));
    }
  }

  // The picked files are read before the input is cleared: read inside the state update, which runs
  // later, they came from a list the clearing had already emptied, and nothing attached ever showed.
  const addFiles = (incoming: Iterable<File>) => {
    const picked = Array.from(incoming);
    if (picked.length) setFiles((held) => [...held, ...picked]);
  };
  const openFile = (file: File) => {
    const url = URL.createObjectURL(file);
    window.open(url, "_blank", "noopener");
    window.setTimeout(() => URL.revokeObjectURL(url), 60_000);
  };

  // The voice note, as the chat's composer has it: the words land after what is typed, or go at
  // once with ↑. Read at the moment they arrive, not when the recording began.
  const draftNow = useRef(draft);
  draftNow.current = draft;
  const note = useVoiceNote({
    sessionId: "",
    asr,
    onWords: (words, go) => {
      const landed = landWords(draftNow.current, words, go);
      if (landed.send) void send(landed.text);
      else {
        setDraft(landed.text);
        window.setTimeout(() => field.current?.focus(), 0);
      }
    },
    onAttach: (file) => addFiles([file]),
    toast,
  });
  const voiceBar = note.state.phase === "recording" || note.state.phase === "transcribing";
  const chooseEffort = (value: string) => setEffort(effortOf(value));
  const textarea = (
    <textarea
      hidden={voiceBar}
      ref={field}
      value={draft}
      rows={1}
      placeholder={t("session.composer.idle")}
      aria-label={t("session.composer.idle")}
      onChange={(event) => setDraft(event.target.value)}
      onPaste={(event) => {
        const pasted = Array.from(event.clipboardData?.items ?? []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter((file): file is File => !!file);
        if (!pasted.length) return;
        event.preventDefault();
        addFiles(pasted);
      }}
      onKeyDown={(event) => {
        if (event.key === "Enter" && !event.shiftKey && enterSends()) {
          event.preventDefault();
          void send();
        }
      }}
    />
  );

  const empty = !draft.trim() && files.length === 0;
  const plusItems = (
    <>
      <button type="button" role="menuitem" onClick={() => { setPlusOpen(false); fileInput.current?.click(); }}><Icon name="attach" size={16} />{t("session.attach")}</button>
      {asr?.configured && <button type="button" role="menuitem" disabled={!note.supported || note.state.phase !== "idle"} onClick={() => { setPlusOpen(false); void note.start(); }}><Icon name="mic" size={16} />{t("session.mic")}</button>}
    </>
  );

  if (!phone) {
    return (
      <div className="start-composer composer" data-primary="send" onDragOver={(event) => { if (event.dataTransfer?.types.includes("Files")) event.preventDefault(); }} onDrop={(event) => { if (!event.dataTransfer?.files.length) return; event.preventDefault(); addFiles(event.dataTransfer.files); }}>
        <VoiceNoteFailed note={note} />
        <div className={`composer-box ${voiceBar ? "voicing" : ""}`}>
          {voiceBar && <VoiceBar note={note} />}
          {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
          {files.length > 0 && !voiceBar && (
            <div className="attachments" aria-label={t("session.attachments")}>
              {files.map((file, i) => (
                <AttachmentCard key={`${file.name}-${file.size}-${file.lastModified}-${i}`} file={file} onOpen={() => openFile(file)} onRemove={() => setFiles((held) => held.filter((_, j) => j !== i))} />
              ))}
            </div>
          )}
          {textarea}
          <div className="composer-row" hidden={voiceBar}>
            <input ref={fileInput} type="file" multiple hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
            <button ref={plusButton} type="button" className="iconbtn flat plus" aria-haspopup="menu" aria-expanded={plusOpen} onClick={() => setPlusOpen(!plusOpen)} aria-label={t("composer.plus")} title={t("composer.plus")}><Icon name="plus" /></button>
            {plusOpen && <Popover anchor={plusButton.current} onClose={() => setPlusOpen(false)} className="plus-menu" label={t("composer.plus")}>{plusItems}</Popover>}
            <div className="composer-tools">
              <RunOnSelect run={run} />
              <ModelSelect model={modelLabel} fallback={null} open={modelOpen} onOpenChange={setModelOpen} onChoose={choose} sheet={phone}
                effort={shownEffort.thinking ? shownEffort.effort : undefined} thinking={shownEffort.thinking} onChooseEffort={chooseEffort} />
              {asr?.configured && <MicButton note={note} />}
              <button type="button" className="roundbtn primary" onClick={() => void send()} disabled={busy || (!draft.trim() && files.length === 0)} aria-label={t("session.send")}><Icon name="up" /></button>
            </div>
          </div>
        </div>
      </div>
    );
  }

  // A phone: one 48 px row at rest, the text above a toolbar once the field has the reader or holds
  // something (ui/phone.css reads data-shape). The white circle is a voice conversation while the
  // field is empty and Send once it is not; offline the draft waits here and Send waits with it.
  const shape = focused || !empty || progress !== null ? "open" : "idle";
  return (
    <div className="start-composer composer" data-primary="send" data-shape={shape} data-typed={draft.trim() ? "" : undefined}
      onFocus={focus.onFocus}
      onBlur={focus.onBlur}>
      <VoiceNoteFailed note={note} />
      <div className={`composer-box ${voiceBar ? "voicing" : ""}`} onClick={(e) => { if (e.target === e.currentTarget) field.current?.focus(); }}>
        {voiceBar && <VoiceBar note={note} />}
        {progress !== null && <div className="sub upload-progress" role="status">{t("upload.progress", { percent: Math.floor(progress * 100) })}</div>}
        {files.length > 0 && !voiceBar && (
          <div className="attachments" aria-label={t("session.attachments")}>
            {files.map((file, i) => (
              <AttachmentCard key={`${file.name}-${file.size}-${file.lastModified}-${i}`} file={file} onOpen={() => openFile(file)} onRemove={() => setFiles((held) => held.filter((_, j) => j !== i))} />
            ))}
          </div>
        )}
        {textarea}
        <div className="composer-row" hidden={voiceBar}>
          <input ref={fileInput} type="file" multiple hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
          <input ref={photoInput} type="file" accept="image/*" capture="environment" hidden onChange={(event) => { addFiles(event.target.files ?? []); event.target.value = ""; }} />
          <button ref={plusButton} type="button" className="iconbtn flat plus" aria-haspopup="dialog" aria-expanded={plusOpen} onClick={() => setPlusOpen(!plusOpen)} aria-label={t("composer.plus")} title={t("composer.plus")}><Icon name="plus" /></button>
          {offline && shape === "open" && <span className="grow ph-hint">{t("ph.offline.kept")}</span>}
          <div className="composer-tools">
            <ModelSelect model={modelLabel} fallback={null} open={modelOpen} onOpenChange={setModelOpen} onChoose={choose} sheet
              effort={shownEffort.thinking ? shownEffort.effort : undefined} thinking={shownEffort.thinking} onChooseEffort={chooseEffort} />
            {asr?.configured && <MicButton note={note} />}
            {empty && !offline
              ? <VoiceCircle />
              : <button type="button" className="roundbtn primary" onClick={() => void send()} disabled={busy || offline || empty} aria-label={t("session.send")}><Icon name="up" /></button>}
          </div>
        </div>
      </div>
      {plusOpen && (
        <PlusSheet onClose={() => setPlusOpen(false)}
          onPhoto={() => photoInput.current?.click()}
          onFiles={() => fileInput.current?.click()}
          onRecord={asr?.configured && note.supported && note.state.phase === "idle" ? () => void note.start() : undefined}
          extra={<><RunOnRow run={run} onDone={() => setPlusOpen(false)} /><SheetRow icon="settings" label={t("ph.plus.options")} hint={t("ph.plus.options.hint")} chevron onClick={() => { setPlusOpen(false); navigate(pathFor("agents", null, { new: "1" })); }} /></>} />
      )}
    </div>
  );
}
