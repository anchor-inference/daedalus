// Settings → Memory: whether memory learns from chats, whether the agent may search them, the model
// and the daily budget of the background pass, what its last pass did and "update now", pause and reset; the sensitive subjects behind their
// master switch; the categorising model and the tidying; import and export. The list itself lives on
// the Memory screen.

import { useRef, useState } from "react";
import { api, MemoryStatus, MemoryTidy, SENSITIVE_SUBJECTS, Settings } from "../api";
import { plural, t } from "../i18n";
import { learningLines, tidyLines } from "../memory/model";
import { ImportSheet, downloadExport } from "../memory/transfer";
import { NumRow, Row } from "../settingsrow";
import { invalidate, useQuery } from "../store";
import { confirmAsync, errorText } from "../ui";
import { Sheet, toast } from "../ui/dialogs";
import { Dropdown, Switch } from "../ui/index";
import { undoToast } from "../memory/undo";

type MemorySection = NonNullable<Settings["memory"]>;

const DEFAULTS: Required<MemorySection> = {
  classify_preset: "", sensitive_allowed: [], consolidate: true, auto_merge: true,
  search_chats: true, generate: true, synthesis_preset: "", synthesis_usd_per_day: 0.5, paused: false,
};

/** The word the reset sheet asks for, in the language the page is in; the host takes either. */
const RESET_WORD = () => t("settings.memory.reset.word");

export function MemorySettings({ s, save }: { s: Settings; save: (patch: Partial<Settings>) => void }) {
  const memory = { ...DEFAULTS, ...(s.memory ?? {}) };
  // The whole section goes back each time: the host merges a section's fields into what it has, so a
  // list sent alone replaces the list, which is what turning one subject off must do.
  const put = (patch: Partial<MemorySection>) => save({ memory: { ...memory, ...patch } });
  const presets = Object.entries(s.presets ?? {});
  const presetOptions = presets.map(([id, p]) => ({ id, label: p.label || `${p.provider}/${p.model}` }));
  const missing = !!memory.classify_preset && !(memory.classify_preset in (s.presets ?? {}));
  const synthesisMissing = !!memory.synthesis_preset && !(memory.synthesis_preset in (s.presets ?? {}));
  // The master switch is the list being non-empty; "on" with nothing chosen yet is this page's own
  // state, shown until a subject is picked or the page is left.
  const [revealed, setRevealed] = useState(false);
  const sensitiveOn = revealed || memory.sensitive_allowed.length > 0;
  const [importing, setImporting] = useState(false);
  const [resetting, setResetting] = useState(false);

  /** Stop allowing subjects: say how many notes memory holds on them, and remove those on a yes
   *  (claude.ai's rule). The setting changes either way; a no keeps the notes. */
  async function disallow(subjects: string[], next: string[]) {
    put({ sensitive_allowed: next });
    if (!subjects.length) return;
    try {
      const { count } = await api.post<{ count: number }>("/api/memory/sensitive", { subjects });
      if (!count) return;
      if (!(await confirmAsync(plural("settings.memory.sensitive.purge.title", count), { body: t("settings.memory.sensitive.purge.body"), action: t("settings.memory.sensitive.purge") }))) return;
      const res = await api.post<{ removed: number; history_id: number | null }>("/api/memory/sensitive", { subjects, apply: true });
      undoToast(plural("memory.forgotten", res.removed), res.history_id, () => invalidate("/api/memory"));
      invalidate("/api/memory");
    } catch (e) {
      toast(errorText(e));
    }
  }

  return (
    <>
      <div className="card memory-settings-main">
        <Row title={t("settings.memory.generate")} desc={t("settings.memory.generate.sub")} data-setting="memory.generate">
          <Switch checked={memory.generate} label={t("settings.memory.generate")} onChange={(generate) => put({ generate })} />
        </Row>
        <Row title={t("settings.memory.search")} desc={t("settings.memory.search.sub")} data-setting="memory.search_chats">
          <Switch checked={memory.search_chats} label={t("settings.memory.search")} onChange={(search_chats) => put({ search_chats })} />
        </Row>
        <Row title={t("settings.memory.paused")} desc={t("settings.memory.paused.sub")} data-setting="memory.paused">
          <Switch checked={memory.paused} label={t("settings.memory.paused")} onChange={(paused) => put({ paused })} />
        </Row>
        <Row title={t("settings.memory.synthesis")} desc={t("settings.memory.synthesis.sub")} stack data-setting="memory.synthesis_preset">
          <Dropdown
            id="memory-synthesis"
            label={t("settings.memory.synthesis")}
            value={memory.synthesis_preset}
            invalid={synthesisMissing}
            onChange={(synthesis_preset) => put({ synthesis_preset })}
            options={[{ id: "", label: t("settings.memory.synthesis.default") }, ...presetOptions, ...(synthesisMissing ? [{ id: memory.synthesis_preset, label: t("settings.compaction.model.missing", { id: memory.synthesis_preset }) }] : [])]}
          />
        </Row>
        <NumRow id="memory-budget" setting="memory.synthesis_usd_per_day" title={t("settings.memory.budget")} desc={t("settings.memory.budget.sub")} unit="$"
          value={memory.synthesis_usd_per_day} min={0} step="0.05" onSave={(synthesis_usd_per_day) => put({ synthesis_usd_per_day })} />
      </div>
      <LearningCard />
      <div className="card memory-settings" data-setting="memory.sensitive_allowed">
        <Row title={t("settings.memory.sensitive.master")} desc={t("settings.memory.sensitive.master.sub")} data-setting="memory.sensitive">
          <Switch checked={sensitiveOn} label={t("settings.memory.sensitive.master")} onChange={(on) => { setRevealed(on); if (!on) void disallow(memory.sensitive_allowed, []); }} />
        </Row>
        {sensitiveOn && (
          <>
            <div className="sub memory-sensitive-sub">{t("settings.memory.sensitive.sub")}</div>
            {SENSITIVE_SUBJECTS.map((subject) => {
              const on = memory.sensitive_allowed.includes(subject);
              return (
                <Row key={subject} title={t(`settings.memory.subject.${subject}`)} desc={t(`settings.memory.subject.${subject}.sub`)} data-subject={subject}>
                  <Switch checked={on} label={t(`settings.memory.subject.${subject}`)} onChange={(next) => (next ? put({ sensitive_allowed: [...memory.sensitive_allowed, subject] }) : void disallow([subject], memory.sensitive_allowed.filter((x) => x !== subject)))} />
                </Row>
              );
            })}
          </>
        )}
      </div>
      <div className="card">
        <Row title={t("settings.memory.classify")} desc={t("settings.memory.classify.sub")} stack data-setting="memory.classify_preset">
          <Dropdown
            id="memory-classify"
            label={t("settings.memory.classify")}
            value={memory.classify_preset}
            invalid={missing}
            onChange={(classify_preset) => put({ classify_preset })}
            options={[{ id: "", label: t("settings.memory.classify.none") }, ...presetOptions, ...(missing ? [{ id: memory.classify_preset, label: t("settings.compaction.model.missing", { id: memory.classify_preset }) }] : [])]}
          />
        </Row>
        <Row title={t("settings.memory.tidy")} desc={t("settings.memory.tidy.sub")} data-setting="memory.consolidate">
          <Switch checked={memory.consolidate} label={t("settings.memory.tidy")} onChange={(consolidate) => put({ consolidate })} />
        </Row>
        <Row title={t("settings.memory.automerge")} desc={t("settings.memory.automerge.sub")} data-setting="memory.auto_merge">
          <Switch checked={memory.auto_merge} disabled={!memory.consolidate} label={t("settings.memory.automerge")} onChange={(auto_merge) => put({ auto_merge })} />
        </Row>
        <TidyNow />
      </div>
      <div className="card">
        <Row title={t("settings.memory.import")} desc={t("settings.memory.import.sub")} data-setting="memory.import">
          <button className="btn small" data-action="memory-import" onClick={() => setImporting(true)}>{t("memory.import.start")}</button>
        </Row>
        <Row title={t("settings.memory.export")} desc={t("settings.memory.export.sub")} data-setting="memory.export">
          <button className="btn small" data-action="memory-export" onClick={() => downloadExport().catch((e) => toast(errorText(e)))}>{t("memory.export")}</button>
        </Row>
        <Row title={t("settings.memory.reset")} desc={t("settings.memory.reset.sub")} data-setting="memory.reset">
          <button className="btn small danger" data-action="memory-reset" onClick={() => setResetting(true)}>{t("settings.memory.reset.action")}</button>
        </Row>
      </div>
      {importing && <ImportSheet onClose={() => setImporting(false)} onDone={() => { setImporting(false); invalidate("/api/memory"); }} />}
      {resetting && <ResetSheet onClose={() => setResetting(false)} />}
    </>
  );
}

/** What the background pass did last and on which model, today's spend, and "update now". The status
 *  is asked for again every few seconds while a pass is reading, so the counts arrive without a reload. */
function LearningCard() {
  // Polled while a pass reads, and for a while after "update now" (the pass may not have started yet).
  const [watchUntil, setWatchUntil] = useState(0);
  const wasRunning = useRef(false);
  const { data, refresh } = useQuery<MemoryStatus>("/api/memory/status", { pollMs: wasRunning.current || Date.now() < watchUntil ? 4000 : 0 });
  const [busy, setBusy] = useState(false);
  const running = !!data && (!!data.running || data.learning > 0);
  wasRunning.current = running;
  async function learnNow(again = false) {
    // Reading every chat again is for recovering what a pass dropped; it costs a pass over the whole
    // backlog, so it asks first. What memory holds already is merged, not written twice.
    if (again && !(await confirmAsync(t("settings.memory.learning.reread.title"), { body: t("settings.memory.learning.reread.body"), action: t("settings.memory.learning.reread.action") }))) return;
    setBusy(true);
    try {
      const res = await api.post<{ started: boolean }>(again ? "/api/memory/synthesis/reread" : "/api/memory/synthesis/run", {});
      toast(t(res.started ? "settings.memory.learning.started" : "settings.memory.learning.already"));
      setWatchUntil(Date.now() + 30_000);
      refresh();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <div className="card memory-learning" data-setting="memory.learning">
      <Row title={t("settings.memory.learning")} desc={t("settings.memory.learning.sub")}>
        <button className="btn small" data-action="memory-learn-now" disabled={busy || running || !!data?.blocked} onClick={() => void learnNow()}>{t("settings.memory.learning.now")}</button>
      </Row>
      <Row title={t("settings.memory.learning.reread")} desc={t("settings.memory.learning.reread.sub")} data-setting="memory.reread">
        <button className="btn small ghost" data-action="memory-reread" disabled={busy || running || !!data?.blocked} onClick={() => void learnNow(true)}>{t("settings.memory.learning.reread.action")}</button>
      </Row>
      <ul className="memory-learning-lines">
        {learningLines(data).map((line, i) => <li key={i} data-tone={line.tone || undefined}>{line.text}</li>)}
      </ul>
    </div>
  );
}

/** "Tidy memory now": a dry run says what would change (and lists the test notes it would delete), the
 *  operator confirms, and the real run is one change with Undo. */
function TidyNow() {
  const [busy, setBusy] = useState(false);
  async function tidy() {
    setBusy(true);
    try {
      const preview = await api.post<MemoryTidy>("/api/memory/tidy", { dry_run: true });
      const lines = tidyLines(preview);
      if (!lines.length) {
        toast(t("settings.memory.tidynow.nothing"));
        return;
      }
      const probes = preview.probe_list.length ? `\n\n${t("settings.memory.tidynow.probelist")}\n${preview.probe_list.map((p) => `• ${p.text}`).join("\n")}` : "";
      if (!(await confirmAsync(t("settings.memory.tidynow.title"), { body: lines.join("\n") + probes, action: t("settings.memory.tidynow.action") }))) return;
      const done = await api.post<MemoryTidy>("/api/memory/tidy", { dry_run: false });
      undoToast(t("settings.memory.tidynow.done"), done.history_id, () => invalidate("/api/memory"));
      invalidate("/api/memory");
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Row title={t("settings.memory.tidynow")} desc={t("settings.memory.tidynow.sub")} data-setting="memory.tidy_now">
      <button className="btn small ghost" data-action="memory-tidy-now" disabled={busy} onClick={() => void tidy()}>{t("settings.memory.tidynow.action")}</button>
    </Row>
  );
}

/** Reset asks for the word typed out: it deletes every note and the history that could bring them back. */
function ResetSheet({ onClose }: { onClose: () => void }) {
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const word = RESET_WORD();
  async function reset() {
    setBusy(true);
    try {
      const res = await api.post<{ deleted: { records: number } }>("/api/memory/reset", { confirm: typed.trim() });
      toast(plural("settings.memory.reset.done", res.deleted.records));
      invalidate("/api/memory");
      onClose();
    } catch (e) {
      toast(errorText(e));
    } finally {
      setBusy(false);
    }
  }
  return (
    <Sheet title={t("settings.memory.reset.title")} onClose={onClose} size="narrow" className="memory-reset">
      <div className="sub">{t("settings.memory.reset.body")}</div>
      <label className="field" htmlFor="memory-reset-word">{t("settings.memory.reset.type", { word })}</label>
      <input id="memory-reset-word" className="field" autoFocus value={typed} onChange={(e) => setTyped(e.target.value)} />
      <div className="sheet-foot">
        <span className="grow" />
        <button className="btn ghost" onClick={onClose}>{t("common.cancel")}</button>
        <button className="btn danger" data-action="reset-confirm" disabled={busy || typed.trim().toLocaleLowerCase() !== word.toLocaleLowerCase()} onClick={() => void reset()}>{t("settings.memory.reset.action")}</button>
      </div>
    </Sheet>
  );
}
