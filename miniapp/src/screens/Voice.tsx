// Voice: the operator talks, a small fast model answers out loud, and the work goes to agents.
//
// The page is a conversation, not a transcript reader. One thing is in the middle of it — an orb that
// breathes when nothing is happening, swells with the operator's own voice while it listens, turns
// and brightens while the concierge thinks, and ripples in time with the answer while it is spoken.
// Under it are the words: what was asked, what is being heard, and the reply a sentence at a time.
// Beside it are the agents the concierge started, each one a tap away from its own session.
//
// The middle of the page is a view, though, and the conversation is not. Reading the transcript of
// what has been said — the concierge's, or any agent's — swaps what is drawn there and touches
// nothing else: the microphone stays open, the answer goes on being read out, the stream is not
// reconnected and not a word of what was being recognised is lost. That is why none of those things
// is in this file any more. They are in `voicesession.ts`, which outlives every view here, and this
// component subscribes to it the way it would to any other store.
//
// The motion is not decoration. There is no other signal on this page: the microphone is open or it
// is not, the model is loaded or it is loading, the answer is being written or being read out, and a
// person who is talking rather than reading has to know which from the corner of their eye. Where the
// reader asked for less of it — `prefers-reduced-motion` — the colours and the words still say all
// four, and the global rule in styles.css takes the animation away.

import { Suspense, lazy, useCallback, useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { api } from "../api";
import { Dropdown, StatusLabel, timeAgo } from "../components";
import { Row } from "../settingsrow";
import { t, useLang } from "../i18n";
import { Icon } from "../icons";
import { pathFor, sessionPath } from "../router";
import { PageHeader, go, screenTitle } from "../shell";
import { useQuery } from "../store";
import { errorText, haptic } from "../ui";
import { localListenSupported } from "../stt";
import { SpeechRuntimeNotice } from "./Components";
import type { AgentNews, VoiceCenter, VoicePreset, VoiceUi } from "../voice";
import { agentNote, micReady, modelRow, orbVisual, recognitionSupported, recorderSupported, smoothLevel } from "../voice";
import { holdVoiceSession, voiceSession } from "../voicesession";
import { usePresenceScope } from "../presence";

// The session view is most of the app's weight — the timeline, the markdown, the windowing, the
// composer — and the voice page is a page that has to be on the screen before the first sentence of
// an answer arrives. Loaded when a transcript is first opened, which is the first moment it is worth
// anything, and never on the way to the orb.
const SessionScreen = lazy(() => import("./Session").then((m) => ({ default: m.SessionScreen })));

type Agent = AgentNews;
type VoiceState = {
  enabled: boolean;
  session_id: string;
  /** The model in use, as it is written on a page: a preset's label, or provider/model when it has none. */
  model: string;
  /** The preset the operator chose; "" is "whatever the default is", which `using` resolves. */
  preset?: string;
  using?: string;
  presets?: VoicePreset[];
  tts?: {
    configured: boolean;
    reason?: string;
    voice?: string;
    model?: string;
    /** Which of the three speaks, decided by the server and merely followed here, as with `stt`. */
    kind?: string;
    /** Where a local voice is in its loading: `loading`, `ready`, `error` — `ready` for the other two. */
    state?: string;
    loaded_in_ms?: number;
    error?: string;
    /** The last answer's wait between being written and being heard, as the server measured its half. */
    last_turn?: { turn: string; first_audio_ms: number; clip_ms: number; load_ms: number };
  };
  stt?: {
    configured: boolean;
    reason?: string;
    /** Which of the three recognisers listens, decided by the server and merely followed here. */
    kind?: string;
    /** Where the local model's weights are: `loading`, `ready`, `error` — `ready` for everything else. */
    state?: string;
    loaded_in_ms?: number;
    error?: string;
    /** The model that runs on this machine, where one is installed and selected. */
    local?: { model: string; label: string; installed: boolean; active: boolean; streaming: boolean; loaded: string };
  };
  agents?: Agent[];
  listening: boolean;
};

export function VoiceScreen({ onOpen, toast }: { onOpen: (id: string) => void; toast: (t: string) => void }) {
  const { data: state, refresh } = useQuery<VoiceState>("/api/voice", { staleMs: 10000, pollMs: 60000 });
  // The concierge's own session is what this page shows, whether or not its transcript is open.
  usePresenceScope({ session: state?.session_id || undefined });
  // The conversation. Not built here and not torn down here: this component is one of the things
  // that can be drawn over it, and it holds the session only for as long as it is on the screen.
  const session = useMemo(() => voiceSession(), []);
  useEffect(() => holdVoiceSession(), []);
  const ui = useSyncExternalStore(session.subscribe, session.state);
  const [typed, setTyped] = useState("");
  // Whether the agents are showing on a phone, where a transcript takes the whole screen and they
  // have nowhere to stand beside it. On a wide window the panel is always there and this is unused.
  const [panel, setPanel] = useState(false);
  useLang();
  // The server produces the audio for both of the first two: a voice on this machine and a speech
  // endpoint both answer /api/voice/tts, and only the browser's own synthesiser does not.
  const serverTts = !!state?.tts?.configured;
  // A voice that runs on this machine is not ready the moment it is chosen: it is a second or two of
  // building a synthesiser, and the process warms it when it starts, when it is chosen and when this
  // page is opened. Until it says ready, this page does not claim it can speak with it.
  const ttsLoading = state?.tts?.kind === "local" && state?.tts?.state === "loading";
  // What the page calls that, which is a third thing: a voice running here is not the server's, and
  // the recognition half of this very page has said so about its own three for a unit already.
  const ttsKind = state?.tts?.kind ?? (serverTts ? "endpoint" : "browser");
  const spokenBy =
    ttsKind === "local"
      ? t(ttsLoading ? "voice.out.local.loading" : "voice.out.local", { voice: state?.tts?.voice || "" })
      : ttsKind === "endpoint"
        ? t("voice.out.server")
        : t("voice.out.browser");
  // Which recogniser listens, in the order the server decides and the page merely follows: a model
  // that runs on the server's processor first, then this browser's own recognition, then a recorder
  // whose cut utterances the server transcribes. The local model wins over the browser because it is
  // a deliberate choice the operator made and paid disk for; the browser's is whatever it shipped.
  const localStt = !!state?.stt?.local?.active && localListenSupported();
  const canRecognise = recognitionSupported();
  const canRecord = recorderSupported() && !!state?.stt?.configured;
  const canTalk = localStt || canRecognise || canRecord;
  const modelName = state?.stt?.local?.label || state?.stt?.local?.model || "";
  const recogniser = localStt
    ? t("voice.recogniser.local", { label: modelName })
    : canRecognise
      ? t("voice.recogniser.browser")
      : canRecord
        ? t("voice.recogniser.server")
        : "";

  // What the session needs of this reading, handed over rather than read: which engine reads the
  // next answer, and which of the three listeners to build when the microphone is next opened.
  useEffect(() => {
    if (!state) return;
    session.reading({
      known: !!state.tts,
      serverTts: !!state.tts?.configured,
      ttsLoading: state.tts?.kind === "local" && state.tts?.state === "loading",
      localStt: !!state.stt?.local?.active,
    });
  }, [session, state]);

  // The server's half of the timing — how long the first clip took and how much of that was the
  // voice being built — is on /api/voice, and it is only worth reading once an answer has been said.
  useEffect(() => session.onAnswered(() => void refresh()), [session, refresh]);

  useEffect(() => {
    session.dispatch({ type: "agents", agents: state?.agents ?? [] });
  }, [session, state?.agents]);

  // The engines' own state comes from the same place the recogniser choice does, so a page opened
  // while the weights are still loading starts in "loading" rather than in "ready" with a microphone
  // that hears nothing — and the two progress streams, which say when that finishes, are opened once
  // for the session rather than once per view.
  useEffect(() => {
    const stt = state?.stt;
    if (!stt) return;
    session.dispatch({
      type: "engine",
      engine: { state: String(stt.state ?? "ready"), model: stt.local?.model ?? "", loadedInMs: Number(stt.loaded_in_ms ?? 0), error: String(stt.error ?? "") },
    });
  }, [session, state?.stt]);
  useEffect(() => {
    const tts = state?.tts;
    if (!tts) return;
    session.dispatch({
      type: "voice",
      engine: { state: String(tts.kind === "local" ? (tts.state ?? "ready") : "ready"), model: String(tts.voice ?? ""), loadedInMs: Number(tts.loaded_in_ms ?? 0), error: String(tts.error ?? "") },
    });
  }, [session, state?.tts]);
  useEffect(() => {
    session.watchEngines(state?.stt?.kind === "local", state?.tts?.kind === "local");
  }, [session, state?.stt?.kind, state?.tts?.kind]);

  // ── what is in the middle ────────────────────────────────────────────────────────────────
  const center = ui.center;
  const reading = center.view !== "orb";
  const setCenter = useCallback((next: VoiceCenter) => session.dispatch({ type: "center", center: next }), [session]);
  const backToVoice = useCallback(() => setCenter({ view: "orb" }), [setCenter]);
  const openTranscript = useCallback(() => {
    if (!state?.session_id) return;
    setCenter({ view: "transcript" });
  }, [setCenter, state?.session_id]);
  const toggleTranscript = useCallback(() => {
    if (reading) backToVoice();
    else openTranscript();
  }, [backToVoice, openTranscript, reading]);

  // One key for the thing the owner asked to reach without stopping the conversation, and Escape to
  // come back. Read from the physical key rather than the character it produces, so the shortcut is
  // the same key on a Russian layout as on an English one.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey) return;
      const target = e.target as HTMLElement | null;
      const typing = !!target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA" || target.isContentEditable);
      if (typing) return;
      if (e.code === "KeyT") {
        e.preventDefault();
        toggleTranscript();
      } else if (e.key === "Escape" && reading) {
        e.preventDefault();
        backToVoice();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [backToVoice, reading, toggleTranscript]);

  // ── the orb's level ──────────────────────────────────────────────────────────────────────
  //
  // Sixty values a second is not React's business: each one would be a render of a page that has a
  // list on it. The level lives in the session, one animation frame smooths it and writes three
  // custom properties onto whichever control is on the screen — the orb, or the small floating one
  // that replaces it while a transcript is being read — and the component re-renders only when the
  // phase changes.
  const orb = useRef<HTMLButtonElement | null>(null);
  //
  // The loop runs only while there is somebody to see it. A reader who asked for less motion gets the
  // properties written once and the loop never started — the stylesheet pins how the orb looks for
  // them, but the sixty writes a second behind that were still happening and cost more than the
  // drawing did. A hidden tab is the same case, and so is a hidden orb: the conversation goes on
  // behind a transcript, and nothing about that needs an animation frame at all.
  const uiRef = useRef(ui);
  uiRef.current = ui;
  useEffect(() => {
    const still = window.matchMedia("(prefers-reduced-motion: reduce)");
    let frame = 0;
    let shown = 0;
    let last = "";
    const write = () => {
      const visual = orbVisual(uiRef.current.phase, shown);
      const node = orb.current;
      // Three identical writes are three style invalidations for nothing, and an idle page makes the
      // same three sixty times a second.
      const now = `${visual.scale.toFixed(3)} ${visual.glow.toFixed(3)} ${visual.spin.toFixed(2)}`;
      if (!node || now === last) return;
      last = now;
      node.style.setProperty("--orb-scale", visual.scale.toFixed(3));
      node.style.setProperty("--orb-glow", visual.glow.toFixed(3));
      node.style.setProperty("--orb-spin", `${visual.spin.toFixed(2)}s`);
    };
    const paint = () => {
      shown = smoothLevel(shown, session.level());
      write();
      frame = requestAnimationFrame(paint);
    };
    const settle = () => {
      if (frame) cancelAnimationFrame(frame);
      frame = 0;
      // A control swapped for another one is a different node with no properties written on it yet.
      last = "";
      if (still.matches || document.hidden) {
        // Written once so the custom properties have values at all: the stylesheet reads them even
        // where it overrides what they do.
        shown = 0;
        write();
        return;
      }
      frame = requestAnimationFrame(paint);
    };
    settle();
    document.addEventListener("visibilitychange", settle);
    still.addEventListener("change", settle);
    return () => {
      if (frame) cancelAnimationFrame(frame);
      document.removeEventListener("visibilitychange", settle);
      still.removeEventListener("change", settle);
    };
  }, [session, reading]);

  const send = useCallback(
    (text: string) => {
      void session.send(text);
    },
    [session],
  );
  const toggleMic = useCallback(() => {
    if (ui.micOn) session.stopMic();
    else void session.startMic();
  }, [session, ui.micOn]);

  async function newConversation() {
    try {
      await session.clear();
      await refresh();
    } catch (e) {
      session.dispatch({ type: "problem", message: errorText(e) });
    }
  }

  if (state && !state.enabled) {
    return (
      <>
        <PageHeader title={<VoiceTitle />} />
        <div className="screen wide">
          <div className="empty">
            <b>{t("voice.off.title")}</b>
            <div>{t("voice.off.body")}</div>
          </div>
        </div>
      </>
    );
  }

  const ready = micReady(ui);
  const agents = ui.agents;
  // Only the one question the page can act on: an installation with no model quick enough to hold a
  // conversation is told where to get one, rather than left to wonder why every answer is late.
  const model = modelRow(state);
  const hint = !ready
    ? t("voice.loading.model", { name: modelName || t("voice.phase.loading") })
    : ui.engine.state === "error"
      ? t("voice.loading.failed", { name: modelName, error: ui.engine.error })
      : ttsKind === "local" && ui.voice.state === "loading"
        ? t("voice.loading.voice")
        : ttsKind === "local" && ui.voice.state === "error"
          ? t("voice.loading.voice.failed", { error: ui.voice.error })
          : !canTalk
            ? t("voice.tap.none")
            : ui.micOn
              ? t("voice.tap.stop")
              : t("voice.tap");
  const composer = (
    <form
      className="voice-compose"
      onSubmit={(e) => {
        e.preventDefault();
        const text = typed;
        setTyped("");
        send(text);
      }}
    >
      <input className="field" placeholder={t("voice.compose")} value={typed} onChange={(e) => setTyped(e.target.value)} aria-label={t("voice.compose.label")} />
      <button className="btn ghost" type="submit" disabled={!typed.trim()} aria-label={t("voice.compose.label")}>
        <Icon name="send" size={16} />
      </button>
    </form>
  );
  const agentTitle = center.view === "agent" ? center.title : "";
  return (
    <>
      <PageHeader
        title={<VoiceTitle />}
        subtitle={state ? `${state.model} · ${spokenBy}` : "…"}
        actions={
          <>
            {state?.session_id && (
              <button className={`btn ghost small${reading ? " on" : ""}`} onClick={toggleTranscript} aria-pressed={reading} title={t("voice.transcript.key")}>
                {t(reading ? "voice.transcript.back" : "voice.transcript")}
              </button>
            )}
            <button className="btn small" onClick={() => void newConversation()}>
              {t("voice.new")}
            </button>
          </>
        }
      />
      <div className="screen wide voice">
        <div className={`voice-grid${reading ? " reading" : ""}${panel ? " panel-open" : ""}`}>
          {/* One area, two things that can be in it. The key is what makes the swap a cross-fade
              rather than a cut — a fresh node for the arriving view — and it is the only thing about
              this change that the conversation behind it can see at all. */}
          <div className={`voice-centre phase-${ui.phase}`}>
            <div className="voice-centre-view" key={center.view === "agent" ? `agent-${center.id}` : center.view}>
              {center.view === "orb" ? (
                <section className={`voice-stage card phase-${ui.phase}`}>
                  <div className="voice-chips">
                    <PhaseChip ui={ui} />
                    <span className="chip quiet">{spokenBy}</span>
                  </div>

                  {/* The page where the absence is felt: the chip above says "the browser's own" and
                      this says what would change that, with the size on the button. It renders
                      nothing once the runtime is installed. */}
                  <SpeechRuntimeNotice toast={toast} onInstalled={refresh} />

                  <div className="voice-orb-wrap">
                    <button
                      ref={orb}
                      className={`voice-orb ${ui.micOn ? "on" : ""}`}
                      onClick={toggleMic}
                      disabled={!canTalk || !ready}
                      aria-pressed={ui.micOn}
                      aria-label={ui.micOn ? t("voice.mic.stop") : t("voice.mic.start")}
                    >
                      <span className="orb-halo" aria-hidden />
                      <span className="orb-shell" aria-hidden />
                      <span className="orb-sweep" aria-hidden />
                      <span className="orb-ring" aria-hidden />
                      <span className="orb-ring two" aria-hidden />
                      <span className="orb-glyph" aria-hidden>
                        <Icon name="mic" size={36} />
                      </span>
                    </button>
                  </div>

                  <div className="voice-hint sub">{hint}</div>

                  <Captions ui={ui} />

                  {/* Where the time went, for the one person who can do something about it. It is a
                      measurement of the last answer and nothing else: how long from the sentence
                      being written to a sound, and how much of that was the voice being built rather
                      than speaking. A warm voice makes the second number disappear, which is the
                      point. */}
                  {/* Not while something is loading: the last answer's "the voice is loaded" then sat
                      under "Loading…" and the page said both things at once. */}
                  {ready && ui.voice.state !== "loading" && <SpokenTiming ms={ui.firstAudioMs} turn={state?.tts?.last_turn} />}

                  {/* There is no asking a browser whether it will make a sound; it is found out by
                      handing it a sentence and hearing nothing start. When that happens the page
                      says so and offers the one thing that fixes it, which is a tap — the same tap
                      the microphone needs. */}
                  {ui.blocked && (
                    <div className="voice-blocked">
                      <span>{t("voice.sound.blocked")}</span>
                      <button
                        className="btn small"
                        onClick={() => {
                          session.unlock();
                          session.dispatch({ type: "blocked", on: false });
                          haptic("light");
                        }}
                      >
                        {t("voice.sound.enable")}
                      </button>
                    </div>
                  )}

                  {composer}
                  <div className="sub voice-why">{canTalk ? t("voice.listening.with", { what: recogniser }) : t("voice.recogniser.none")}</div>
                  {model.addFast && (
                    <div className="sub voice-why">
                      {t("voice.card.model.none")}{" "}
                      <a href={pathFor("settings", "models")} onClick={(e) => go(e, pathFor("settings", "models"))}>
                        {t("voice.card.model.add")}
                      </a>
                    </div>
                  )}
                </section>
              ) : (
                <section className={`card voice-read${center.view === "transcript" ? " own" : ""}`}>
                  <div className="voice-crumbs">
                    <button className="iconbtn" onClick={backToVoice} aria-label={t("voice.transcript.back")} title={t("voice.transcript.back")}>
                      <Icon name="back" />
                    </button>
                    <nav className="voice-crumb" aria-label={t("voice.crumb.label")}>
                      <button className="link" onClick={backToVoice}>
                        {screenTitle("voice")}
                      </button>
                      <span aria-hidden>›</span>
                      <b className="truncate">{center.view === "agent" ? agentTitle : t("voice.crumb.own")}</b>
                    </nav>
                    <button className="btn ghost small phone-only voice-panel-key" onClick={() => setPanel((v) => !v)} aria-pressed={panel}>
                      {t("voice.agents")}
                      {agents.length ? ` · ${agents.length}` : ""}
                    </button>
                    {center.view === "agent" && (
                      <a
                        className="btn ghost small wide-only"
                        href={sessionPath(center.id)}
                        onClick={(e) => {
                          // The agent's own full page is still one tap away, and taking it leaves
                          // the voice page — which ends the conversation, as leaving always has.
                          e.preventDefault();
                          onOpen(center.id);
                        }}
                      >
                        {t("voice.agent.open")}
                      </a>
                    )}
                  </div>
                  <div className="voice-read-body">
                    <Suspense fallback={<div className="sub voice-read-wait">{t("common.loading")}</div>}>
                      <SessionScreen id={center.view === "agent" ? center.id : state?.session_id || ""} onBack={backToVoice} onOpen={onOpen} toast={toast} />
                    </Suspense>
                  </div>
                  {/* The concierge's own transcript has no composer of its own here: what is said to
                      the concierge is said out loud, or typed into the field the voice page has
                      always had, and two composers a centimetre apart that send to the same session
                      is a question the operator should not have to answer. An agent's transcript
                      keeps its own, because typing to an agent is the reason for opening it. */}
                  {center.view === "transcript" && (
                    <div className="voice-read-foot">
                      <Captions ui={ui} />
                      {composer}
                    </div>
                  )}
                </section>
              )}
            </div>

            {/* The microphone, while the orb is not on the screen: bottom right on a wide window and
                bottom centre on a phone, the same six states in the same six colours, and a
                miniature of the same motion driven by the same three properties. */}
            {reading && (
              <button
                ref={orb}
                className={`voice-mic-float phase-${ui.phase} ${ui.micOn ? "on" : ""}`}
                onClick={toggleMic}
                disabled={!canTalk || !ready}
                aria-pressed={ui.micOn}
                aria-label={ui.micOn ? t("voice.mic.stop") : t("voice.mic.start")}
                title={t(`voice.phase.${ui.phase}`)}
              >
                <span className="orb-halo" aria-hidden />
                <span className="orb-shell" aria-hidden />
                <span className="orb-sweep" aria-hidden />
                <span className="orb-ring" aria-hidden />
                <span className="orb-glyph" aria-hidden>
                  <Icon name="mic" size={20} />
                </span>
                <span className="voice-mic-word">{t(`voice.phase.${ui.phase}`)}</span>
              </button>
            )}
          </div>

          <aside className="voice-agents">
            <h2 className="voice-agents-head">
              {t("voice.agents")}
              <span className="sub">{agents.length ? ` · ${agents.length}` : ""}</span>
            </h2>
            {agents.length === 0 && <div className="sub voice-agents-empty">{t("voice.agents.empty")}</div>}
            {agents.map((a) => {
              const note = agentNote(a);
              const open = center.view === "agent" && center.id === a.session_id;
              return (
                <button
                  key={a.session_id}
                  className={`card row pressable voice-agent${open ? " open" : ""}`}
                  aria-pressed={open}
                  onClick={() => {
                    setPanel(false);
                    setCenter(open ? { view: "orb" } : { view: "agent", id: a.session_id, title: a.title });
                  }}
                >
                  <div className="grow">
                    <div className="voice-agent-top">
                      <b className="truncate">{a.title}</b>
                      {note.waiting ? <span className="chip accent">{t(note.waiting)}</span> : <StatusLabel status={a.status} />}
                    </div>
                    {note.line && <div className={`sub clamp-2${note.live ? " voice-agent-live" : ""}`}>{note.line}</div>}
                    <div className="sub num">{timeAgo(note.when)}</div>
                  </div>
                </button>
              );
            })}
          </aside>
        </div>
      </div>
    </>
  );
}

/** What was asked, what is being heard, and the answer a sentence at a time. */
function Captions({ ui }: { ui: VoiceUi }) {
  return (
    <div className="voice-captions" aria-live="polite">
      {ui.asked && <p className="voice-asked">{ui.asked}</p>}
      {ui.heard && <p className="voice-heard">{ui.heard}</p>}
      <div className="voice-said">
        {ui.spoken.map((sentence, i) => (
          <span className={`voice-sentence${ui.unspoken.includes(sentence) ? " unspoken" : ""}`} key={`${i}-${sentence.slice(0, 12)}`}>
            {sentence}
            {ui.unspoken.includes(sentence) && <i className="voice-unspoken">{t("voice.unspoken")}</i>}{" "}
          </span>
        ))}
        {!ui.spoken.length && ui.partial && <span className="voice-sentence writing">{ui.partial}</span>}
        {/* The breathing mark stands where the answer is about to appear, so it is drawn only while
            one is awaited. Drawn at rest too, it was a bare bar under the orb — above it on a phone —
            that read as a grab handle belonging to nothing. */}
        {!ui.spoken.length && !ui.partial && (ui.phase === "thinking" || ui.phase === "delegating") && <span className="voice-waiting" aria-hidden />}
      </div>
      {ui.problem && <p className="voice-problem">{ui.problem}</p>}
    </div>
  );
}

/** The one word for what the page is doing, and the animation that says it without being read. */
function PhaseChip({ ui }: { ui: VoiceUi }) {
  const word = t(`voice.phase.${ui.phase}`);
  return (
    <span className={`chip voice-chip ${ui.phase}`}>
      <span className="voice-chip-mark" aria-hidden>
        <i />
        <i />
        <i />
      </span>
      {word}
      {ui.delegating && `: ${ui.delegating}`}
    </span>
  );
}

/**
 * How long the last answer took to be heard, and where that time went.
 *
 * Small, quiet, and only ever a measurement. The page measures the half the operator feels — from
 * the sentence arriving to a sound — and the server measures its own: what the request producing the
 * first clip spent, and how much of that was the voice being built. Nothing is drawn before an
 * answer has been spoken, because a diagnostic line full of zeroes reads as a fault.
 */
function SpokenTiming({ ms, turn }: { ms: number; turn?: { first_audio_ms: number; clip_ms: number; load_ms: number } }) {
  const felt = ms || turn?.first_audio_ms || 0;
  if (!felt) return null;
  const seconds = (value: number) => (value / 1000).toFixed(1);
  const parts: string[] = [];
  if (turn?.load_ms) parts.push(t("voice.timing.load", { ms: seconds(turn.load_ms) }));
  else if (turn?.clip_ms) parts.push(t("voice.timing.ready"));
  if (turn?.clip_ms) parts.push(t("voice.timing.synth", { ms: seconds(Math.max(0, turn.clip_ms - (turn.load_ms || 0))) }));
  return (
    <div className="voice-timing sub" aria-live="off">
      {t("voice.timing", { ms: seconds(felt) })}
      {parts.length > 0 && ` · ${parts.join(" · ")}`}
    </div>
  );
}

function VoiceTitle() {
  return (
    <>
      {screenTitle("voice")} <span className="chip accent voice-beta">{t("voice.beta")}</span>
    </>
  );
}

/** Which of the three speaks, in one line: the same order /api/voice decides and the chip follows. */
function spokenLine(data: VoiceState): string {
  const tts = data.tts;
  if (tts?.kind === "local") return t("voice.card.out.local", { voice: tts.voice || "" });
  if (tts?.configured) return t("voice.card.out.server", { model: tts.model || "", voice: tts.voice || "" });
  return tts?.reason || t("voice.card.out.browser");
}

/** The Settings card: what the page runs on and what it can and cannot do here. */
export function VoiceSettings({ toast }: { toast: (t: string) => void }) {
  const { data, refresh } = useQuery<VoiceState>("/api/voice", { staleMs: 10000 });
  const [saving, setSaving] = useState(false);
  const [problem, setProblem] = useState("");
  // The model is chosen here because this is where the operator came looking for it. It is the one
  // thing on this card that is written rather than reported, and the reading that follows the write
  // is the server's, not the app's guess at what it did.
  const pick = async (preset: string) => {
    setSaving(true);
    setProblem("");
    try {
      await api.put("/api/voice/model", { preset });
      await refresh();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setSaving(false);
    }
  };
  if (!data) return <div className="sub">{t("common.loading")}</div>;
  const row = modelRow(data);
  const local = data.stt?.local;
  const listens = local?.active
    ? t(local.streaming ? "voice.card.in.local.streaming" : "voice.card.in.local", { label: local.label })
    : recognitionSupported()
      ? t("voice.card.in.browser")
      : data.stt?.configured
        ? t("voice.card.in.server")
        : t("voice.card.in.none");
  return (
    <div className="card voice-card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("voice.card.title")}</div>
      <div className="sub">{t("voice.card.intro")}</div>
      <Row title={t("voice.card.enabled")}>
        <span className="settings-value">{t(data.enabled ? "voice.card.enabled.yes" : "voice.card.enabled.no")}</span>
      </Row>
      <Row
        title={t("voice.card.model")}
        desc={
          <>
            {t("voice.card.model.hint")} {row.warn && <span className="chip attn">{t(row.warn)}</span>}
            {row.addFast && (
              <>
                {" "}{t("voice.card.model.none")}{" "}
                <a href={pathFor("settings", "models")} onClick={(e) => go(e, pathFor("settings", "models"))}>{t("voice.card.model.add")}</a>
              </>
            )}
            {problem && <span className="push-error"> {problem}</span>}
          </>
        }
        stack
      >
        <Dropdown
          label={t("voice.card.model")}
          value={row.value}
          disabled={saving}
          onChange={(preset) => void pick(preset)}
          options={[
            { id: "", label: row.fallback ? t("voice.card.model.default.named", { model: row.fallback }) : t("voice.card.model.default") },
            ...row.choices.map((c) => ({ id: c.id, label: c.label, hint: c.slow ? t("voice.card.model.option.slow", { detail: c.detail }) : c.detail })),
          ]}
        />
      </Row>
      <Row title={t("voice.card.out")} stack>
        <span className="settings-value">{spokenLine(data)}</span>
      </Row>
      <Row title={t("voice.card.in")} stack>
        <span className="settings-value">{listens}</span>
      </Row>
      {local?.active && (
        <Row title={t("voice.card.memory")}>
          <span className="settings-value">
            {data.stt?.state === "loading"
              ? t("voice.card.memory.loading")
              : data.stt?.state === "error"
                ? data.stt.error || t("voice.card.memory.failed")
                : data.stt?.loaded_in_ms
                  ? t("voice.card.memory.ready", { s: (data.stt.loaded_in_ms / 1000).toFixed(1) })
                  : t("voice.card.memory.later")}
          </span>
        </Row>
      )}
      {/* Where the two lines above say "the browser's own" because nothing else is installed, the
          offer to install it belongs directly under them rather than on another page. */}
      <SpeechRuntimeNotice toast={toast} onInstalled={refresh} />
      <div className="btnrow">
        <a className="btn small" href={pathFor("voice")} onClick={(e) => go(e, pathFor("voice"))}>
          {t("voice.card.open")}
        </a>
      </div>
    </div>
  );
}
