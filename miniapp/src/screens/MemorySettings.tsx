// Settings → Memory: which sensitive subjects Remember may keep, the model that names a memory's
// category and topics, and whether the background tidying runs and applies what it is sure of.

import { SENSITIVE_SUBJECTS, Settings } from "../api";
import { t } from "../i18n";
import { Row } from "../settingsrow";
import { Dropdown, Switch } from "../ui/index";

type MemorySection = NonNullable<Settings["memory"]>;

const DEFAULTS: MemorySection = { extract_after_run: false, extract_min_messages: 12, extract_preset: "", classify_preset: "", sensitive_allowed: [], consolidate: true, auto_merge: true };

export function MemorySettings({ s, save }: { s: Settings; save: (patch: Partial<Settings>) => void }) {
  const memory = { ...DEFAULTS, ...(s.memory ?? {}) };
  // The whole section goes back each time: the host merges a section's fields into what it has, so a
  // list sent alone replaces the list, which is what turning one subject off must do.
  const put = (patch: Partial<MemorySection>) => save({ memory: { ...memory, ...patch } });
  const presets = Object.entries(s.presets ?? {});
  const missing = !!memory.classify_preset && !(memory.classify_preset in (s.presets ?? {}));
  return (
    <>
      <div className="card memory-settings" data-setting="memory.sensitive_allowed">
        <div className="section-title" style={{ marginTop: 0 }}>{t("settings.memory.sensitive")}</div>
        <div className="sub">{t("settings.memory.sensitive.sub")}</div>
        {SENSITIVE_SUBJECTS.map((subject) => {
          const on = memory.sensitive_allowed.includes(subject);
          return (
            <Row key={subject} title={t(`settings.memory.subject.${subject}`)} desc={t(`settings.memory.subject.${subject}.sub`)} data-subject={subject}>
              <Switch checked={on} label={t(`settings.memory.subject.${subject}`)} onChange={(next) => put({ sensitive_allowed: next ? [...memory.sensitive_allowed, subject] : memory.sensitive_allowed.filter((x) => x !== subject) })} />
            </Row>
          );
        })}
      </div>
      <div className="card">
        <Row title={t("settings.memory.classify")} desc={t("settings.memory.classify.sub")} stack data-setting="memory.classify_preset">
          <Dropdown
            id="memory-classify"
            label={t("settings.memory.classify")}
            value={memory.classify_preset}
            invalid={missing}
            onChange={(classify_preset) => put({ classify_preset })}
            options={[{ id: "", label: t("settings.memory.classify.none") }, ...presets.map(([id, p]) => ({ id, label: p.label || `${p.provider}/${p.model}` })), ...(missing ? [{ id: memory.classify_preset, label: t("settings.compaction.model.missing", { id: memory.classify_preset }) }] : [])]}
          />
        </Row>
        <Row title={t("settings.memory.tidy")} desc={t("settings.memory.tidy.sub")} data-setting="memory.consolidate">
          <Switch checked={memory.consolidate} label={t("settings.memory.tidy")} onChange={(consolidate) => put({ consolidate })} />
        </Row>
        <Row title={t("settings.memory.automerge")} desc={t("settings.memory.automerge.sub")} data-setting="memory.auto_merge">
          <Switch checked={memory.auto_merge} disabled={!memory.consolidate} label={t("settings.memory.automerge")} onChange={(auto_merge) => put({ auto_merge })} />
        </Row>
      </div>
    </>
  );
}
