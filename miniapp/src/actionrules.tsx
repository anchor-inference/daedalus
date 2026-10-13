// Settings → Rules: how much of what it does in the world the agent decides on its own.
//
// One row per category of action (sending to people, publishing, paying…), one choice per row of four
// levels from "does it" to "hands it to you", and whether a small model reviews the action against
// your request before it runs. The levels are the host's `[action_rules]` section and are saved with
// the rest of the settings; the categories, the levels and the review's log come from
// `GET /api/action-rules`, so a category the host adds appears here without a new build.
//
// On a wide screen the categories and levels are a grid of radio buttons, which reads as the table it
// is. On a phone four columns of words do not fit beside a name, so each category becomes a name with
// its four choices under it; the markup is the same and only the layout changes.

import "./actionrules.css";
import type { ActionRulesSettings, Settings } from "./api";
import { DICT, t } from "./i18n";
import { useQuery } from "./store";
import { Row } from "./settingsrow";
import { Dropdown, Switch, timeAgo } from "./ui/components";
import { go } from "./ui/index";

export type ActionRulesInfo = {
  categories: string[];
  levels: string[];
  defaults: Record<string, string>;
  log: ReviewEntry[];
};

export type ReviewEntry = {
  id: number;
  at: string;
  session_id: string;
  tool: string;
  category: string;
  level: string;
  verdict: "allow" | "block" | "ask";
  reason: string;
  failed: boolean;
  summary: string;
  model: string;
  elapsed_ms: number;
};

export const CATEGORIES = ["message", "publish", "payment", "delete", "external_write", "self_change"];
export const LEVELS = ["auto", "on_command", "ask", "handoff"];
export const DEFAULT_RULES: ActionRulesSettings = {
  levels: { message: "auto", publish: "auto", payment: "ask", delete: "auto", external_write: "auto", self_change: "auto" },
  review: ["message", "publish", "payment"],
  review_preset: "",
  review_timeout_seconds: 20,
  injection_model: false,
  injection_preset: "",
};

/** The words for a category or level the host knows and this build does not: its own id. */
function word(key: string, fallback: string): string {
  return key in DICT ? t(key) : fallback;
}

/** Whether the review stands before a category at its level: at `auto` when switched on, at
 *  `on_command` always (the review is what checks that you asked), never at `ask` or `handoff`. */
export function reviewState(level: string, reviewed: boolean): "on" | "off" | "always" | "none" {
  if (level === "on_command") return "always";
  if (level === "ask" || level === "handoff") return "none";
  return reviewed ? "on" : "off";
}

export function ActionRules({ s, save }: { s: Settings; save: (patch: Partial<Settings>) => Promise<void> }) {
  const info = useQuery<ActionRulesInfo>("/api/action-rules", { pollMs: 30000, staleMs: 10000 });
  const current: ActionRulesSettings = { ...DEFAULT_RULES, ...(s.action_rules ?? {}), levels: { ...DEFAULT_RULES.levels, ...(s.action_rules?.levels ?? {}) } };
  const categories = info.data?.categories ?? CATEGORIES;
  const levels = info.data?.levels ?? LEVELS;
  const put = (patch: Partial<ActionRulesSettings>) => save({ action_rules: { ...current, ...patch } });
  const setLevel = (category: string, level: string) => void put({ levels: { ...current.levels, [category]: level } });
  const setReview = (category: string, on: boolean) => void put({ review: on ? [...new Set([...current.review, category])] : current.review.filter((c) => c !== category) });
  const presets = Object.keys(s.presets ?? {});
  return (
    <>
      <div className="card action-rules" data-testid="action-rules">
        <div className="section-title" style={{ marginTop: 0 }}>{t("rules.actions.title")}</div>
        <div className="sub">{t("rules.actions.sub")}</div>
        <div className="ar-grid" role="table" aria-label={t("rules.actions.title")}>
          <div className="ar-row ar-head" role="row">
            <span role="columnheader" className="ar-name">{t("rules.actions.category")}</span>
            <span className="ar-levels">
              {levels.map((level) => (
                <span key={level} role="columnheader" className="ar-level-head" title={word(`rules.level.${level}.hint`, "")}>{word(`rules.level.${level}`, level)}</span>
              ))}
            </span>
            <span role="columnheader" className="ar-review-head">{t("rules.review")}</span>
          </div>
          {categories.map((category) => {
            const level = current.levels[category] ?? info.data?.defaults?.[category] ?? "auto";
            const review = reviewState(level, current.review.includes(category));
            const name = word(`rules.cat.${category}`, category);
            return (
              <div key={category} className="ar-row" role="row" data-category={category} data-level={level}>
                <span role="rowheader" className="ar-name">
                  <b>{name}</b>
                  <span className="sub">{word(`rules.cat.${category}.hint`, "")}</span>
                </span>
                <div className="ar-levels" role="radiogroup" aria-label={name}>
                  {levels.map((choice) => (
                    <button
                      key={choice}
                      type="button"
                      role="radio"
                      aria-checked={level === choice}
                      data-level={choice}
                      className={`ar-cell ${level === choice ? "on" : ""}`}
                      title={word(`rules.level.${choice}.hint`, "")}
                      onClick={() => level !== choice && setLevel(category, choice)}
                    >
                      <span className="ar-dot" aria-hidden />
                      <span className="ar-cell-label">{word(`rules.level.${choice}`, choice)}</span>
                    </button>
                  ))}
                </div>
                <span className="ar-review" data-review={review}>
                  <Switch checked={review === "on" || review === "always"} disabled={review === "always" || review === "none"} onChange={(on) => setReview(category, on)} label={t("rules.review.of", { category: name })} />
                  <span className="ar-review-label sub">{t(`rules.review.${review}`)}</span>
                </span>
              </div>
            );
          })}
        </div>
        <div className="sub ar-legend">{t("rules.actions.legend")}</div>
      </div>
      <div className="card">
        <div className="section-title" style={{ marginTop: 0 }}>{t("rules.review.title")}</div>
        <div className="sub">{t("rules.review.sub")}</div>
        <Row title={t("rules.review.model")} desc={t("rules.review.model.hint")} stack data-setting="action_rules.review_preset">
          <Dropdown id="action-review-model" label={t("rules.review.model")} value={current.review_preset} onChange={(review_preset) => void put({ review_preset })} options={[{ id: "", label: t("rules.model.auto") }, ...presets.map((p) => ({ id: p, label: p }))]} />
        </Row>
        <Row title={t("rules.injection")} desc={t("rules.injection.sub")}>
          <Switch checked={current.injection_model} onChange={(injection_model) => void put({ injection_model })} label={t("rules.injection")} />
        </Row>
      </div>
      <ReviewLog entries={info.data?.log ?? null} />
    </>
  );
}

function ReviewLog({ entries }: { entries: ReviewEntry[] | null }) {
  return (
    <div className="card ar-log" data-testid="review-log">
      <div className="section-title" style={{ marginTop: 0 }}>{t("rules.log.title")}</div>
      <div className="sub">{t("rules.log.sub")}</div>
      {entries === null ? (
        <div className="empty">{t("common.loading")}</div>
      ) : entries.length === 0 ? (
        <div className="empty">{t("rules.log.empty")}</div>
      ) : (
        <ul className="ar-log-list">
          {entries.map((e) => (
            <li key={e.id} className="ar-log-entry" data-verdict={e.verdict}>
              <div className="ar-log-head">
                <span className={`ar-verdict ${e.verdict}`}>{t(`rules.verdict.${e.verdict}`)}</span>
                <b className="ar-log-what">{word(`rules.cat.${e.category}`, e.category)}</b>
                <span className="mono ar-log-tool">{e.tool}</span>
                <time className="sub" dateTime={e.at}>{timeAgo(e.at)}</time>
              </div>
              {e.summary && <div className="mono ar-log-summary">{e.summary}</div>}
              <div className="ar-log-reason">
                {e.failed && <span className="attn">{t("rules.verdict.failed")} </span>}
                {e.reason}
              </div>
              {e.session_id && (
                <a className="ar-log-link sub" href={`/app/agents/${e.session_id}`} onClick={(ev) => go(ev, `/app/agents/${e.session_id}`)}>{t("rules.log.open")}</a>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
