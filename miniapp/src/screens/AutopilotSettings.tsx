// Settings → Chat: autopilot and the style profile, the two things that act on the operator's behalf.
//
// Autopilot's configuration sits beside what it spent today and what it decided, because the allowance
// means nothing without the number it is measured against. The style profile is shown as the host
// learned it and can only be forgotten, not edited: the next update would write over an edit, and a
// profile the operator wrote themselves is a prompt, which has its own place.

import { useCallback, useEffect, useState } from "react";
import { api, type AutopilotOverview, type Settings, type UserStyle } from "../api";
import { plural, t } from "../i18n";
import { NumRow, Row } from "../settingsrow";
import { Dropdown, Switch } from "../ui/index";
import { confirmAsync, errorText } from "../ui";
import { timeAgo } from "../ui/components";
import { usd } from "../format";

type SaveProps = { s: Settings; save: (patch: Partial<Settings>) => void; presetLabel: (id: string) => string };

export function AutopilotSettings({ s, save, presetLabel }: SaveProps) {
  const pilot = s.autopilot ?? { enabled: false, preset: "", max_continuations: 3, usd_per_day: 0.2 };
  const put = (patch: Partial<typeof pilot>) => save({ autopilot: { ...pilot, ...patch } });
  const [overview, setOverview] = useState<AutopilotOverview | null>(null);
  useEffect(() => {
    api.get<AutopilotOverview>("/api/autopilot").then(setOverview).catch(() => setOverview(null));
  }, [pilot.enabled, pilot.usd_per_day]);
  const presets = Object.keys(s.presets ?? {});
  const verdicts = overview?.verdicts_today ?? {};
  return (
    <div className="card" data-section="autopilot">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.autopilot")}</div>
      <Row title={t("settings.autopilot.enabled")} desc={t("settings.autopilot.enabled.sub")} data-setting="autopilot.enabled">
        <Switch checked={pilot.enabled} onChange={(enabled) => put({ enabled })} label={t("settings.autopilot.enabled")} />
      </Row>
      <Row title={t("settings.autopilot.preset")} desc={t("settings.autopilot.preset.sub")} stack data-setting="autopilot.preset">
        <Dropdown id="autopilot-preset" label={t("settings.autopilot.preset")} value={pilot.preset ?? ""} onChange={(preset) => put({ preset })}
          options={[{ id: "", label: t("settings.autopilot.preset.same") }, ...presets.map((p) => ({ id: p, label: presetLabel(p) }))]} />
      </Row>
      <NumRow id="autopilot-turns" setting="autopilot.max_continuations" title={t("settings.autopilot.turns")} desc={t("settings.autopilot.turns.sub")}
        value={pilot.max_continuations} min={0} max={20} step={1} onSave={(max_continuations) => put({ max_continuations })} />
      <NumRow id="autopilot-usd" setting="autopilot.usd_per_day" title={t("settings.autopilot.usd")} desc={t("settings.autopilot.usd.sub")} unit="$"
        value={pilot.usd_per_day} min={0} step="0.05" onSave={(usd_per_day) => put({ usd_per_day })} />
      {overview && (
        <div className="sub autopilot-today" data-autopilot-today>
          {t("settings.autopilot.today", {
            spent: usd(overview.spent_today),
            cap: usd(overview.usd_per_day),
            sessions: plural("settings.autopilot.sessions", overview.sessions_on),
          })}
          {" · "}
          {t("settings.autopilot.verdicts", {
            go: String(verdicts.continue ?? 0),
            done: String(verdicts.done ?? 0),
            asked: String((verdicts.ask ?? 0) + (verdicts.choice ?? 0)),
            skipped: String(overview.skipped_today),
          })}
        </div>
      )}
    </div>
  );
}

export function UserStyleCard({ toast }: { toast: (text: string) => void }) {
  const [style, setStyle] = useState<UserStyle | null>(null);
  const load = useCallback(() => {
    api.get<UserStyle>("/api/user-style").then(setStyle).catch(() => setStyle(null));
  }, []);
  useEffect(load, [load]);
  async function forget() {
    if (!(await confirmAsync(t("settings.style.forget.confirm")))) return;
    try {
      setStyle(await api.delete<UserStyle>("/api/user-style"));
      toast(t("settings.style.forgotten"));
    } catch (e) {
      toast(errorText(e));
    }
  }
  return (
    <div className="card" data-section="user-style">
      <div className="section-title" style={{ marginTop: 0 }}>{t("settings.style")}</div>
      <Row title={t("settings.style.profile")} desc={t("settings.style.profile.sub")} data-setting="user_style">
        <button type="button" className="btn" disabled={!style?.summary} onClick={() => void forget()}>{t("settings.style.forget")}</button>
      </Row>
      {style?.summary
        ? <pre className="style-profile" data-style-profile>{style.summary}</pre>
        : <div className="sub style-empty" data-style-empty>{t("settings.style.empty")}</div>}
      {style?.summary && (
        <div className="sub style-learned">
          {t("settings.style.learned", { count: plural("settings.style.messages", style.learned), when: style.updated_at ? timeAgo(style.updated_at) : "" })}
        </div>
      )}
    </div>
  );
}
