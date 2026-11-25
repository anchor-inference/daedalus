// Choosing the voice the answer is read in.
//
// A synthesiser is not chosen from a table. Nobody can tell two Russian voices apart by their size,
// their licence or a quality bar — they can only be told apart by listening to them — so every row
// here has a Play button that synthesises one sentence in the card's own language, through the very
// engine that will read the answers, before anything is chosen. Everything else on the card is there
// to narrow the field before that: the language, whether the voice is male or female, what it costs
// on disk, and whether it can keep up with a person talking, which two of them cannot.
//
// Only one voice is in use at a time; several can be installed, and the one in use is a tap away.

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { modelSize as size } from "../format";
import { plural, t } from "../i18n";
import { mergeTtsView } from "../ttsview";
import type { TtsView } from "../ttsview";
import { errorText, haptic } from "../ui";
import { Badge, Button, IconButton, Select, Switch, confirmDialog } from "../ui/primitives";
import { NumInput, Row, SetBlock, SetGroup } from "../settingsrow";
import { CatalogueProgress, CatalogueRow, Meter } from "./catalogue";

const name = (code: string) => t(`lang.of.${code}`);

export function TtsVoices() {
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
          await api.post("/api/stt/engine");
        }
        await api.post(`/api/tts/voices/${encodeURIComponent(id)}/download`);
      } else if (what === "cancel") {
        await api.post(`/api/tts/voices/${encodeURIComponent(id)}/cancel`);
      } else if (what === "use") {
        take(await api.post<Partial<TtsView>>("/api/tts/select", { voice: id }));
        haptic("medium");
        return;
      } else {
        await api.delete(`/api/tts/voices/${encodeURIComponent(id)}`);
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

  if (!view) return <SetGroup title={t("tts.title")} data-setting="companion.tts.local_voice"><SetBlock><span className="sub">{problem || t("common.loading")}</span></SetBlock></SetGroup>;

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

  async function remove(id: string, label: string, bytes: number) {
    const sure = await confirmDialog({ title: t("tts.delete.title", { name: label }), body: t("tts.delete.body", { size: size(bytes) }), action: t("tts.delete"), danger: true });
    if (sure) await act(id, "delete");
  }

  return (
    <>
      <SetGroup title={t("tts.title")} help={t("tts.intro")}>
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
        {view.state.error && <SetBlock><span className="sub attn">{view.state.error}</span></SetBlock>}
      </SetGroup>

      <SetGroup title={t("tts.catalogue")} note={t("tts.catalogue.count", { shown: shown.length, n: view.models.length })} data-setting="companion.tts.local_voice">
        {/* The filters are the first block of the card the rows are in, so narrowing the list and the
            list it narrows are one thing. */}
        <SetBlock className="cat-filters">
          <input className="field" placeholder={t("tts.search")} aria-label={t("tts.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
          <Select label={t("tts.language")} value={language} onChange={setLanguage} options={[{ id: "", label: t("tts.language.any") }, ...view.languages.map((code) => ({ id: code, label: name(code) }))]} />
          <span className="cat-switch">
            <span>{t("tts.fast.only")}</span>
            <Switch checked={fastOnly} onChange={setFastOnly} label={t("tts.fast.only")} />
          </span>
        </SetBlock>
        {problem && <SetBlock><span className="sub attn">{problem}</span></SetBlock>}
        {shown.map((m) => {
          const progress = m.progress;
          const downloading = progress && ["downloading", "verifying", "unpacking"].includes(progress.state);
          const queued = progress?.state === "queued";
          const recommended = language ? m.recommended_for.includes(language) : m.recommended_for.length > 0;
          // A voice filed under Russian that also reads English has to say so, or the only way to find
          // out is to download it: the language badge alone reads as a single-language voice.
          const also = m.languages.filter((code) => code !== m.language);
          return (
            <CatalogueRow
              key={m.id}
              data-voice={m.id}
              using={m.selected}
              name={m.label}
              badges={
                <>
                  {m.selected && <Badge tone="accent">{t("tts.card.using")}</Badge>}
                  {recommended && <Badge tone="live">{language ? t("tts.recommended.for", { lang: name(language) }) : t("tts.recommended")}</Badge>}
                  <Badge>{t(`tts.gender.${m.gender}`)}</Badge>
                  <Badge>{name(m.language)}</Badge>
                  {m.new && <Badge tone="accent">{t("tts.new")}</Badge>}
                  {/* Only the exception is coloured. Two of these voices render slower than a person
                      talks, which is the one fact on the row that changes how the page feels. */}
                  {!m.keeps_up && <Badge tone="attention">{t("tts.slow")}</Badge>}
                </>
              }
              note={m.note}
              facts={[
                t("tts.facts.size", { dl: size(m.size_bytes), disk: size(m.disk_bytes), mem: m.memory_mb }),
                t("tts.facts.rate", { khz: Math.round(m.sample_rate / 1000), licence: m.licence }),
                m.speakers.length > 1 ? plural("tts.facts.speakers", m.speakers.length) : "",
                also.length > 0 ? t("tts.facts.languages", { langs: also.map(name).join(", ") }) : "",
              ].filter(Boolean).join(" · ")}
              meters={
                <>
                  <Meter label={t("tts.quality")} value={m.quality} />
                  <Meter label={t("tts.speed")} value={m.speed} />
                </>
              }
              status={
                <>
                  {downloading && (
                    <CatalogueProgress
                      fraction={progress?.fraction ?? 0}
                      label={progress?.state === "downloading" ? `${Math.round((progress?.fraction ?? 0) * 100)}%` : t(`tts.progress.${progress?.state}`)}
                    />
                  )}
                  {progress?.state === "failed" && <div className="cat-note attn">{progress.error || t("tts.failed")}</div>}
                  {queued && <div className="cat-note">{t("tts.queued")}</div>}
                </>
              }
              actions={
                downloading ? (
                  <Button size="sm" onClick={() => void act(m.id, "cancel")}>{t("tts.cancel")}</Button>
                ) : m.installed ? (
                  <>
                    <Button size="sm" icon="play" disabled={playing === m.id} onClick={() => void sample(m.id, language)}>
                      {playing === m.id ? t("tts.playing") : t("tts.play")}
                    </Button>
                    {m.selected ? (
                      <Button size="sm" onClick={() => void act("", "use")}>{t("tts.stop")}</Button>
                    ) : (
                      <Button size="sm" variant="primary" disabled={busy === m.id} onClick={() => void act(m.id, "use")}>{t("tts.use")}</Button>
                    )}
                    <IconButton icon="trash" label={t("tts.delete")} size={28} disabled={busy === m.id} onClick={() => void remove(m.id, m.label, m.disk_bytes)} />
                  </>
                ) : (
                  <Button size="sm" variant="primary" icon="download" disabled={busy === m.id || (busyElsewhere && !queued)} onClick={() => void act(m.id, "download")}>
                    {t("tts.download", { size: size(m.size_bytes) })}
                  </Button>
                )
              }
            />
          );
        })}
        {!shown.length && <SetBlock><span className="sub">{t("tts.nomatch", { n: view.models.length })}</span></SetBlock>}
      </SetGroup>

      {chosen && (
        <SetGroup title={t("tts.settings")}>
          {chosen.speakers.length > 1 && (
            <Row title={t("tts.speaker")} data-setting="companion.tts.local_speaker">
              <Select label={t("tts.speaker")} value={view.state.speaker} onChange={(speaker) => void settings({ speaker })} options={[{ id: "", label: t("tts.speaker.first") }, ...chosen.speakers.map((who) => ({ id: who, label: who }))]} />
            </Row>
          )}
          {/* The number beside the thumb follows the drag; the configuration file is written when the
              drag ends. Saving on every change was a POST and a full rewrite of the operator's TOML
              per pixel — about thirty of each for one pull from 1.0 to 2.0. */}
          <Row title={t("tts.speed.title")} data-setting="companion.tts.local_speed">
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
          <Row title={t("tts.threads")} data-setting="companion.tts.local_threads">
            <NumInput label={t("tts.threads")} value={view.state.threads} min={1} max={16} onSave={(threads) => void settings({ threads })} />
          </Row>
        </SetGroup>
      )}
    </>
  );
}
