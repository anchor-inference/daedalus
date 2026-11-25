// Choosing a speech model that runs on this machine.
//
// The list is the whole decision, so it is shown as one: every model with what it speaks, what it
// costs in disk and memory, whether it hears words as they are said or only whole sentences, and two
// bars for how well and how fast. Filters narrow it — a language, and streaming only — because
// twelve entries is more than anyone reads and the operator almost always knows which language they
// intend to speak.
//
// Only one model is in use at a time; several can be installed, and the one in use is a tap away from
// any of them.

import { useCallback, useEffect, useState } from "react";
import { api } from "../api";
import { modelSize as size } from "../format";
import { plural, t } from "../i18n";
import { invalidate } from "../store";
import { errorText, haptic } from "../ui";
import { Badge, Button, IconButton, Select, Switch, confirmDialog } from "../ui/primitives";
import { NumInput, Row, SetBlock, SetGroup } from "../settingsrow";
import { CatalogueProgress, CatalogueRow, Meter } from "./catalogue";
import type { SpeechModel, SpeechView } from "../sttview";
import { fetchSttView, mergeSttView, postSttSelect, sttFrame } from "../sttview";

export type { SpeechModel, SpeechView } from "../sttview";

// The same table the synthesis picker beside this one reads: one language is one word, whichever of
// the two lists it came from.
const name = (code: string) => t(`lang.of.${code}`);

function Languages({ model }: { model: SpeechModel }) {
  const shown = model.languages.slice(0, 4).map(name);
  const rest = model.language_count - shown.length;
  return <>{shown.join(", ")}{rest > 0 ? ` ${t("stt.languages.more", { n: rest })}` : ""}</>;
}

/** Where the chosen model is: in memory and quick, on its way there, or refusing to load. */
function LoadLine({ load }: { load: SpeechView["load"] }) {
  if (!load || load.state === "idle") return null;
  if (load.state === "loading") {
    return (
      <Row title={t("stt.memory")}>
        <span className="settings-value stt-loading">{t("stt.memory.loading")}</span>
      </Row>
    );
  }
  if (load.state === "error") {
    return <SetBlock><span className="sub attn">{t("stt.load.failed", { reason: load.error || t("stt.load.refused") })}</span></SetBlock>;
  }
  return (
    <Row title={t("stt.memory")}>
      <span className="settings-value">{load.loaded_in_ms ? t("stt.memory.ready.in", { s: (load.loaded_in_ms / 1000).toFixed(1) }) : t("stt.memory.ready")}</span>
    </Row>
  );
}

export function SpeechModels() {
  const [view, setView] = useState<SpeechView | null>(null);
  const [query, setQuery] = useState("");
  const [language, setLanguage] = useState("");
  const [streamingOnly, setStreamingOnly] = useState(false);
  const [busy, setBusy] = useState("");
  const [problem, setProblem] = useState("");

  /** Every answer is folded into what is already on the screen; none of them replaces it. */
  const take = useCallback((answer: Partial<SpeechView>) => {
    setView((current) => mergeSttView(current, answer));
  }, []);

  const load = useCallback(async () => {
    try {
      take(await fetchSttView());
    } catch (e) {
      setProblem(errorText(e));
    }
  }, [take]);

  useEffect(() => {
    void load();
  }, [load]);

  // Download progress comes as a stream rather than a poll, so a bar that is moving looks like it.
  // A finished or failed download reloads the list, which is where "installed" and the disk total
  // come from; everything in between only moves the bar.
  useEffect(() => {
    const controller = new AbortController();
    void (async () => {
      try {
        const response = await fetch("/api/stt/progress", { headers: api.authHeaders(), signal: controller.signal });
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
              const frame = sttFrame(JSON.parse(data));
              if (!frame) continue;
              if (frame.kind === "engine") {
                // The same stream carries the engine loading a model into memory, which is a card
                // here and the reason the voice page's microphone is shut over there.
                setView((v) => (v ? { ...v, load: frame.load } : v));
                continue;
              }
              const update = frame.progress;
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

  async function act(id: string, what: "download" | "use" | "cancel" | "delete") {
    setBusy(id);
    setProblem("");
    try {
      if (what === "download") {
        // The engine is an optional extra natively; the first download is when it becomes worth
        // installing, so it is fetched before the model rather than after, when it would be missed.
        if (view && !view.engine_installed) {
          await api.post("/api/stt/engine");
        }
        await api.post(`/api/stt/models/${encodeURIComponent(id)}/download`);
      } else if (what === "cancel") {
        await api.post(`/api/stt/models/${encodeURIComponent(id)}/cancel`);
      } else if (what === "use") {
        take(await postSttSelect({ model: id }));
        // The voice page and the card above it both say which recogniser listens; the selection has
        // just changed that, so they are told rather than left until something reloads them.
        invalidate("/api/voice");
        haptic("medium");
        return;
      } else {
        take(await api.delete<Partial<SpeechView>>(`/api/stt/models/${encodeURIComponent(id)}`));
        invalidate("/api/voice");
      }
      await load();
    } catch (e) {
      setProblem(errorText(e));
    } finally {
      setBusy("");
    }
  }

  if (!view) return <SetGroup title={t("stt.title")} data-setting="stt.models"><SetBlock><span className="sub">{problem || t("stt.loading")}</span></SetBlock></SetGroup>;

  const wanted = query.trim().toLowerCase();
  const shown = view.models.filter((m) => {
    if (streamingOnly && !m.streaming) return false;
    if (language && !m.languages.includes(language)) return false;
    if (wanted && !`${m.label} ${m.note} ${m.languages.map(name).join(" ")}`.toLowerCase().includes(wanted)) return false;
    return true;
  });
  const installedCount = view.models.filter((m) => m.installed).length;
  // One model is fetched at a time — half a gigabyte twice over a link that carries neither faster is
  // not a thing to offer, and twelve Download buttons make it easy to ask for by accident. The other
  // rows say so instead of starting a second and being queued.
  const busyElsewhere = view.models.some((m) => m.progress && ["downloading", "verifying", "unpacking"].includes(m.progress.state));
  // Whisper is told its language at load time, so "auto" is English for it and not a detection.
  const autoDetects = view.models.find((m) => m.id === view.selected)?.detects_language ?? true;

  async function remove(id: string, label: string, bytes: number) {
    const sure = await confirmDialog({ title: t("stt.delete.title", { name: label }), body: t("stt.delete.body", { size: size(bytes) }), action: t("stt.delete"), danger: true });
    if (sure) await act(id, "delete");
  }

  return (
    <div id="speech-models">
      <SetGroup title={t("stt.title")} help={t("stt.intro")}>
        <Row title={t("stt.inuse")} data-setting="stt.local_model">
          <span className="settings-value">{view.selected ? view.models.find((m) => m.id === view.selected)?.label ?? view.selected : t("stt.inuse.none")}</span>
        </Row>
        <Row title={t("stt.ondisk")}>
          <span className="settings-value">{installedCount ? plural("stt.ondisk.some", installedCount, { size: size(view.disk_bytes) }) : t("stt.ondisk.none")}</span>
        </Row>
        {view.selected && <LoadLine load={view.load} />}
        {view.selected && !view.decoders?.opus && !view.decoders?.any && (
          <SetBlock><span className="sub attn">{t("stt.nodecoder")}</span></SetBlock>
        )}
      </SetGroup>

      <SetGroup title={t("stt.catalogue")} note={t("stt.catalogue.count", { shown: shown.length, n: view.models.length })} data-setting="stt.models">
        {/* The filters are the first block of the card the rows are in: the search takes the room, the
            language is a compact picker and "streaming only" a labelled switch. */}
        <SetBlock className="cat-filters">
          <input className="field" placeholder={t("stt.search")} aria-label={t("stt.search")} value={query} onChange={(e) => setQuery(e.target.value)} />
          <Select label={t("stt.language")} value={language} onChange={setLanguage} options={[{ id: "", label: t("stt.language.any") }, ...view.languages.map((code) => ({ id: code, label: name(code) }))]} />
          <span className="cat-switch">
            <span>{t("stt.streaming.only")}</span>
            <Switch checked={streamingOnly} onChange={setStreamingOnly} label={t("stt.streaming.only")} />
          </span>
        </SetBlock>
        {problem && <SetBlock><span className="sub attn">{problem}</span></SetBlock>}
        {shown.map((m) => {
          const progress = m.progress;
          const downloading = progress && ["downloading", "verifying", "unpacking"].includes(progress.state);
          const queued = progress?.state === "queued";
          const recommended = language ? m.recommended_for.includes(language) : m.recommended_for.length > 0;
          return (
            <CatalogueRow
              key={m.id}
              data-model={m.id}
              using={m.selected}
              name={m.label}
              badges={
                <>
                  {m.selected && <Badge tone="accent">{t("stt.card.using")}</Badge>}
                  {recommended && <Badge tone="live">{language ? t("stt.recommended.for", { lang: name(language) }) : t("stt.recommended")}</Badge>}
                  {m.streaming ? <Badge tone="live">{t("stt.chip.streaming")}</Badge> : <Badge>{t("stt.chip.sentences")}</Badge>}
                </>
              }
              note={m.note}
              facts={[
                <Languages key="languages" model={m} />,
                t("stt.facts.size", { dl: size(m.size_bytes), disk: size(m.disk_bytes), mem: m.memory_mb }),
                `${t(m.verified ? "stt.facts.checksum" : "stt.facts.nochecksum", { licence: m.licence })}${m.detects_language ? "" : ` · ${t("stt.facts.englishonly")}`}`,
              ].flatMap((fact, i) => (i ? [" · ", fact] : [fact]))}
              meters={
                <>
                  <Meter label={t("stt.accuracy")} value={m.accuracy} />
                  <Meter label={t("stt.speed")} value={m.speed} />
                </>
              }
              status={
                <>
                  {downloading && (
                    <CatalogueProgress
                      fraction={progress?.fraction ?? 0}
                      label={progress?.state === "downloading" ? `${Math.round((progress?.fraction ?? 0) * 100)}%` : t(`stt.progress.${progress?.state}`)}
                    />
                  )}
                  {progress?.state === "failed" && <div className="cat-note attn">{progress.error || t("stt.failed")}</div>}
                  {queued && <div className="cat-note">{t("stt.queued")}</div>}
                </>
              }
              actions={
                downloading ? (
                  <Button size="sm" onClick={() => void act(m.id, "cancel")}>{t("stt.cancel")}</Button>
                ) : m.installed ? (
                  <>
                    {m.selected ? (
                      <Button size="sm" onClick={() => void act("", "use")}>{t("stt.stop")}</Button>
                    ) : (
                      <Button size="sm" variant="primary" disabled={busy === m.id} onClick={() => void act(m.id, "use")}>{t("stt.use")}</Button>
                    )}
                    <IconButton icon="trash" label={t("stt.delete")} size={28} disabled={busy === m.id} onClick={() => void remove(m.id, m.label, m.disk_bytes)} />
                  </>
                ) : (
                  <Button size="sm" variant="primary" icon="download" disabled={busy === m.id || (busyElsewhere && !queued)} onClick={() => void act(m.id, "download")}>
                    {t("stt.download", { size: size(m.size_bytes) })}
                  </Button>
                )
              }
            />
          );
        })}
        {!shown.length && <SetBlock><span className="sub">{t("stt.nomatch", { n: view.models.length })}</span></SetBlock>}
      </SetGroup>

      {view.selected && (
        <SetGroup title={t("stt.settings")}>
          <Row title={t("stt.language")} data-setting="stt.local_language">
            <Select
              label={t("stt.language")}
              value={view.language}
              onChange={(language) => void postSttSelect({ language }).then(take).catch((x) => setProblem(errorText(x)))}
              options={[{ id: "auto", label: t(autoDetects ? "stt.language.auto" : "stt.language.auto.english") }, ...view.languages.map((code) => ({ id: code, label: name(code) }))]}
            />
          </Row>
          <Row title={t("stt.threads")} data-setting="stt.local_threads">
            <NumInput label={t("stt.threads")} value={view.threads} min={1} max={16} onSave={(threads) => void postSttSelect({ threads }).then(take).catch((x) => setProblem(errorText(x)))} />
          </Row>
        </SetGroup>
      )}
    </div>
  );
}
