// Choosing the voice the answer is read in.
//
// A synthesiser is not chosen from a table. Nobody can tell two Russian voices apart by their size,
// their licence or a quality bar — they can only be told apart by listening to them — so every card
// here has a Play button that synthesises one sentence in the card's own language, through the very
// engine that will read the answers, before anything is chosen. Everything else on the card is there
// to narrow the field before that: the language, whether the voice is male or female, what it costs
// on disk, and whether it can keep up with a person talking, which two of them cannot.
//
// Only one voice is in use at a time; several can be installed, and the one in use is a tap away.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Icon } from "../icons";
import { modelSize as size } from "../format";
import { plural, t } from "../i18n";
import { mergeTtsView } from "../ttsview";
import type { TtsView } from "../ttsview";
import { errorText, haptic } from "../ui";
import { Dropdown, Switch } from "../components";
import { NumInput, Row } from "../settingsrow";

const name = (code: string) => t(`lang.of.${code}`);

/** One 0-100 score as a bar. Two of these side by side are the whole comparison most people make. */
function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="stt-bar">
      <span className="sub faint">{label}</span>
      <span className="stt-bar-track">
        <span className="stt-bar-fill" style={{ width: `${Math.max(3, Math.min(100, value))}%` }} />
      </span>
    </div>
  );
}

export function TtsVoices({ toast }: { toast: (message: string) => void }) {
  const [view, setView] = useState<TtsView | null>(null);
  // Every answer is folded into the view the page already has rather than replacing it: three
  // endpoints answer with this shape and a field missing from any of them is a blank screen.
  const take = useCallback((answer: Partial<TtsView> | null | undefined) => setView((v) => mergeTtsView(v, answer)), []);
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [fastOnly, setFastOnly] = useState(false);
  const [busy, setBusy] = useState("");
  const [playing, setPlaying] = useState("");
  const [problem, setProblem] = useState("");
  // One player for the whole list. A second sample starting while the first is still talking would
  // be two voices at once, which is exactly what someone comparing them does not want to hear.
  const player = useRef<HTMLAudioElement | null>(null);

  const load = useCallback(async () => {
    try {
      take(await api.get<Partial<TtsView>>("/api/tts"));
    } catch (e) {
      setProblem(errorText(e));
    }
  }, [take]);

  useEffect(() => {
    void load();
  }, [load]);

  // Stop whatever is talking when the screen goes away, so navigating off the page is silence.
  useEffect(() => () => {
    player.current?.pause();
    if (player.current?.src) URL.revokeObjectURL(player.current.src);
  }, []);

  // Download progress comes as a stream rather than a poll, so a bar that is moving looks like it.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/api/tts/progress", { headers: api.authHeaders(), signal: controller.signal });
        if (!response.body) return;
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        for (;;) {
          const { value, done } = await reader.read();
          if (done) return;
          buffer += decoder.decode(value, { stream: true });
          const frames = buffer.split("\n\n");
          buffer = frames.pop() ?? "";
          for (const frame of frames) {
            const data = /^data: (.*)$/m.exec(frame)?.[1];
            if (!data) continue;
            try {
              const update = JSON.parse(data) as { id: string; state: string; fraction: number; error: string };
              setView((v) => (v ? { ...v, models: v.models.map((m) => (m.id === update.id ? { ...m, progress: update } : m)) } : v));
              if (["installed", "failed", "cancelled", "deleted"].includes(update.state)) void load();
              if (update.state === "failed" && update.error) setProblem(update.error);
            } catch {
              /* one malformed frame must not end the stream */
            }
          }
        }
      } catch {
        /* the page navigated away, or the stream dropped; the list still reloads on every action */
      }
    })();
    return () => controller.abort();
  }, [load]);

  async function sample(id: string, asked: string) {
    player.current?.pause();
    setProblem("");
    setPlaying(id);
    try {
      // The language the picker is filtered by goes with it: a voice that speaks thirty-one of them
      // is filed under one, and playing its own would answer a question the listener did not ask.
      const query = asked ? `?language=${encodeURIComponent(asked)}` : "";
      const response = await fetch(`/api/tts/voices/${encodeURIComponent(id)}/sample${query}`, { method: "POST", headers: api.authHeaders() });
      if (!response.ok) throw new Error(await response.text());
      const url = URL.createObjectURL(await response.blob());
      const audio = new Audio(url);
      player.current = audio;
      const finish = () => {
        URL.revokeObjectURL(url);
        setPlaying((current) => (current === id ? "" : current));
      };
      audio.onended = finish;
      audio.onerror = finish;
      await audio.play();
    } catch (e) {
      setProblem(errorText(e));
      setPlaying("");
    }
  }

  async function act(id: string, what: "download" | "use" | "cancel" | "delete") {
    setBusy(id);
    setProblem("");
    try {
      if (what === "download") {
        // The engine is an optional extra natively, and it is the same wheel recognition uses, so
        // this is the recognition install endpoint on purpose rather than a second copy of it.
        if (view && !view.engine_installed) {
          toast(t("tts.engine.installing"));
          await api.post("/api/stt/engine");
        }
        await api.post(`/api/tts/voices/${encodeURIComponent(id)}/download`);
        toast(t("tts.toast.downloading"));
      } else if (what === "cancel") {
        await api.post(`/api/tts/voices/${encodeURIComponent(id)}/cancel`);
      } else if (what === "use") {
        take(await api.post<Partial<TtsView>>("/api/tts/select", { voice: id }));
        haptic("medium");
        toast(id ? t("tts.toast.using") : t("tts.toast.stopped"));
        return;
      } else {
        await api.delete(`/api/tts/voices/${encodeURIComponent(id)}`);
        toast(t("tts.toast.removed"));
      }
      await load();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy("");
    }
  }

  /** The speed while the thumb is being dragged, before it is worth writing anything to disk. */
  const [dragging, setDragging] = useState<number | null>(null);

  async function commitSpeed(speed: number) {
    setDragging(null);
    if (Math.abs(speed - view!.state.speed) < 0.001) return;
    await settings({ speed });
  }

  async function settings(patch: Record<string, unknown>) {
    try {
      take(await api.post<Partial<TtsView>>("/api/tts/select", patch));
    } catch (e) {
      setProblem(errorText(e));
    }
  }

  if (!view) return <div className="card"><div className="sub">{problem || t("common.loading")}</div></div>;

  const wanted = query.trim().toLowerCase();
  const shown = view.models.filter((m) => {
    if (fastOnly && !m.keeps_up) return false;
    if (language && m.language !== language && !m.languages.includes(language)) return false;
    if (wanted && !`${m.label} ${m.note} ${name(m.language)}`.toLowerCase().includes(wanted)) return false;
    return true;
  });
  const installedCount = view.models.filter((m) => m.installed).length;
  const busyElsewhere = view.models.some((m) => m.progress && ["downloading", "verifying", "unpacking"].includes(m.progress.state));
  const chosen = view.models.find((m) => m.id === view.selected);

  return (
    <div className="card">
      <div className="section-title" style={{ marginTop: 0 }}>{t("tts.title")}</div>
      <div className="sub">{t("tts.intro")}</div>

      <Row title={t("tts.inuse")}>
        <span className="settings-value">
          {chosen ? chosen.label : t("tts.inuse.none")}
          {chosen && view.state.state === "loading" && <span className="faint"> · {t("tts.state.loading")}</span>}
          {chosen && view.state.state === "error" && <span className="attn"> · {t("tts.state.error")}</span>}
        </span>
      </Row>
      <Row title={t("tts.ondisk")}>
        <span className="settings-value">{installedCount ? plural("tts.ondisk.some", installedCount, { size: size(view.disk_bytes) }) : t("tts.ondisk.none")}</span>
      </Row>
      {view.state.error && <div className="sub attn" style={{ marginTop: 6 }}>{view.state.error}</div>}

      <div className="catalogue-filters">
        <input className="field" placeholder={t("tts.search")} aria-label={t("tts.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
        <Dropdown label={t("tts.language")} value={language} onChange={setLanguage} options={[{ id: "", label: t("tts.language.any") }, ...view.languages.map((code) => ({ id: code, label: name(code) }))]} />
        <span className="filter-switch">
          <span>{t("tts.fast.only")}</span>
          <Switch checked={fastOnly} onChange={setFastOnly} label={t("tts.fast.only")} />
        </span>
      </div>

      {problem && <div className="sub attn" style={{ marginTop: 8 }}>{problem}</div>}

      <div className="stt-list">
        {shown.map((m) => {
          const progress = m.progress;
          const downloading = progress && ["downloading", "verifying", "unpacking"].includes(progress.state);
          const queued = progress?.state === "queued";
          const recommended = language ? m.recommended_for.includes(language) : m.recommended_for.length > 0;
          // A card filed under Russian that also reads English has to say so, or the only way to find
          // out is to download it: the language chip alone reads as a single-language voice.
          const also = m.languages.filter((code) => code !== m.language);
          return (
            <div key={m.id} className={`stt-card ${m.selected ? "using" : ""}`}>
              <div className="stt-head">
                <b>{m.label}</b>
                {recommended && <span className="chip accent">{language ? t("tts.recommended.for", { lang: name(language) }) : t("tts.recommended")}</span>}
                <span className="chip">{t(`tts.gender.${m.gender}`)}</span>
                <span className="chip">{name(m.language)}</span>
                {m.new && <span className="chip accent">{t("tts.new")}</span>}
                {/* Only the exception is coloured. Two of these voices render slower than a person
                    talks, which is the one fact on the card that changes how the page feels. */}
                {!m.keeps_up && <span className="chip attn">{t("tts.slow")}</span>}
                {m.selected && <span className="chip accent">{t("tts.card.using")}</span>}
              </div>
              <div className="sub">{m.note}</div>
              <div className="stt-facts sub faint">
                <span>{t("tts.facts.size", { dl: size(m.size_bytes), disk: size(m.disk_bytes), mem: m.memory_mb })}</span>
                <span>{t("tts.facts.rate", { khz: Math.round(m.sample_rate / 1000), licence: m.licence })}</span>
                {m.speakers.length > 1 && <span>{plural("tts.facts.speakers", m.speakers.length)}</span>}
                {also.length > 0 && <span>{t("tts.facts.languages", { langs: also.map(name).join(", ") })}</span>}
              </div>
              <div className="stt-bars">
                <Bar label={t("tts.quality")} value={m.quality} />
                <Bar label={t("tts.speed")} value={m.speed} />
              </div>
              {downloading && (
                <div className="stt-progress">
                  <span className="stt-bar-track">
                    <span className="stt-bar-fill accent" style={{ width: `${Math.round((progress?.fraction ?? 0) * 100)}%` }} />
                  </span>
                  <span className="sub faint">
                    {progress?.state === "downloading" ? `${Math.round((progress?.fraction ?? 0) * 100)}%` : t(`tts.progress.${progress?.state}`)}
                  </span>
                </div>
              )}
              {progress?.state === "failed" && <div className="sub attn">{progress.error || t("tts.failed")}</div>}
              {queued && <div className="sub faint">{t("tts.queued")}</div>}
              <div className="btnrow">
                {downloading ? (
                  <button className="btn small" onClick={() => void act(m.id, "cancel")}>{t("tts.cancel")}</button>
                ) : m.installed ? (
                  <>
                    <button className="btn small" disabled={playing === m.id} onClick={() => void sample(m.id, language)}>
                      <Icon name="mic" size={14} /> {playing === m.id ? t("tts.playing") : t("tts.play")}
                    </button>
                    {m.selected ? (
                      <button className="btn small" onClick={() => void act("", "use")}>{t("tts.stop")}</button>
                    ) : (
                      <button className="btn small primary" disabled={busy === m.id} onClick={() => void act(m.id, "use")}>{t("tts.use")}</button>
                    )}
                    <button className="btn small danger" disabled={busy === m.id} onClick={() => void act(m.id, "delete")}>{t("tts.delete")}</button>
                  </>
                ) : (
                  <button
                    className="btn small primary"
                    disabled={busy === m.id || (busyElsewhere && !queued)}
                    onClick={() => void act(m.id, "download")}
                  >
                    <Icon name="download" size={14} /> {t("tts.download", { size: size(m.size_bytes) })}
                  </button>
                )}
              </div>
            </div>
          );
        })}
        {!shown.length && <div className="sub faint">{t("tts.nomatch", { n: view.models.length })}</div>}
      </div>

      {chosen && (
        <div className="catalogue-settings">
          {chosen.speakers.length > 1 && (
            <Row title={t("tts.speaker")} stack>
              <Dropdown label={t("tts.speaker")} value={view.state.speaker} onChange={(speaker) => void settings({ speaker })} options={[{ id: "", label: t("tts.speaker.first") }, ...chosen.speakers.map((who) => ({ id: who, label: who }))]} />
            </Row>
          )}
          {/* The number beside the thumb follows the drag; the configuration file is written when the
              drag ends. Saving on every change was a POST and a full rewrite of the operator's TOML
              per pixel — about thirty of each for one pull from 1.0 to 2.0. */}
          <Row title={t("tts.speed.title")} stack>
            <span className="settings-range">
              <input
                type="range"
                min={0.5}
                max={2}
                step={0.05}
                aria-label={t("tts.speed.title")}
                defaultValue={view.state.speed}
                onChange={(e) => setDragging(Number(e.target.value))}
                onPointerUp={(e) => void commitSpeed(Number((e.target as HTMLInputElement).value))}
                onKeyUp={(e) => void commitSpeed(Number((e.target as HTMLInputElement).value))}
                onBlur={(e) => void commitSpeed(Number(e.target.value))}
              />
              <span className="settings-unit">{(dragging ?? view.state.speed).toFixed(2)}×</span>
            </span>
          </Row>
          <Row title={t("tts.threads")}>
            <NumInput label={t("tts.threads")} value={view.state.threads} min={1} max={16} onSave={(threads) => void settings({ threads })} />
          </Row>
        </div>
      )}
    </div>
  );
}
