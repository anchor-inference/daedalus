// Settings → Approvals: how much of what it does in the world the agent decides on its own.
//
// One row per category of action (sending to people, publishing, paying…), one choice per row of four
// levels from "does it" to "hands it to you", and whether a small model reviews the action against
// your request before it runs. The levels are the host's `[action_rules]` section and are saved with
// the rest of the settings; the categories, the levels and the review's log come from
// `GET /api/action-rules`, so a category the host adds appears here without a new build.
//
// Each category is a row of the card: its name and what it covers on the left, its level as a list
// that opens at the right and the review's switch beside it. A grid of four radio buttons a row read
// as a table but did not fit a phone, where each category had to be re-laid as a name over four
// buttons; a row of two controls is the same on both.

import "./actionrules.css";
import type { ActionRulesSettings, Settings } from "./api";
import { DICT, t } from "./i18n";
import { useQuery } from "./store";
import { Row, SetBlock, SetGroup } from "./settingsrow";
import { ModelChoice } from "./modelchoice";
import { Select, Switch } from "./ui/primitives";
import { timeAgo } from "./ui/components";
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
  /** ``override``: the operator let a call the review blocked through anyway. */
  verdict: "allow" | "block" | "ask" | "override";
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
  return (
    <>
      <SetGroup title={t("rules.actions.title")} help={t("rules.actions.sub")} className="action-rules" data-testid="action-rules" data-setting="action_rules">
        {categories.map((category) => {
          const level = current.levels[category] ?? info.data?.defaults?.[category] ?? "auto";
          const review = reviewState(level, current.review.includes(category));
          const name = word(`rules.cat.${category}`, category);
          return (
            <Row key={category} className="ar-row" title={name} desc={word(`rules.cat.${category}.hint`, "")} data-category={category} data-level={level}>
              <Select<string> className="set-value ar-level" label={name} value={level} onChange={(choice) => choice !== level && setLevel(category, choice)}
                options={levels.map((choice) => ({ id: choice, label: word(`rules.level.${choice}`, choice), hint: word(`rules.level.${choice}.hint`, "") || undefined }))} />
              <span className="ar-review" data-review={review}>
                <span className="ar-review-label">{t(`rules.review.${review}`)}</span>
                <Switch checked={review === "on" || review === "always"} disabled={review === "always" || review === "none"} onChange={(on) => setReview(category, on)} label={t("rules.review.of", { category: name })} />
              </span>
            </Row>
          );
        })}
        <SetBlock><span className="set-hint">{t("rules.actions.legend")}</span></SetBlock>
      </SetGroup>
      <SetGroup title={t("rules.review.title")} help={t("rules.review.sub")}>
        <Row title={t("rules.review.model")} desc={t("rules.review.model.hint")} stack data-setting="action_rules.review_preset">
          <ModelChoice id="action-review-model" label={t("rules.review.model")} catalogue={s} value={current.review_preset} onChange={(review_preset) => void put({ review_preset })} defaultOption={{ label: t("rules.model.auto") }} />
        </Row>
        <Row title={t("rules.injection")} desc={t("rules.injection.sub")}>
          <Switch checked={current.injection_model} onChange={(injection_model) => void put({ injection_model })} label={t("rules.injection")} />
        </Row>
      </SetGroup>
      <ReviewLog entries={info.data?.log ?? null} />
    </>
  );
}

function ReviewLog({ entries }: { entries: ReviewEntry[] | null }) {
  return (
    <SetGroup title={t("rules.log.title")} help={t("rules.log.sub")} data-testid="review-log">
      {entries === null ? (
        <SetBlock><span className="set-hint">{t("common.loading")}</span></SetBlock>
      ) : entries.length === 0 ? (
        <SetBlock><span className="set-hint">{t("rules.log.empty")}</span></SetBlock>
      ) : (
        entries.map((e) => (
          <div key={e.id} className="set-block ar-log-entry" data-verdict={e.verdict}>
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
          </div>
        ))
      )}
    </SetGroup>
  );
}
