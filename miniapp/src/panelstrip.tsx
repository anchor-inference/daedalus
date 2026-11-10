// The strip over every tab of the right panel: the session's model, how full its context is and what it
// cost, and the menu of what one does with the session. The Details tab used to carry these as the
// first cards of a long column, so on Files or Preview the two numbers the operator watches were a tab
// and a scroll away; here they stay in sight whatever the panel shows.

import type { SessionDetail } from "./api";
import { Icon } from "./icons";
import { OverflowMenu, type MenuItem } from "./ui/dialogs";
import { contextSegments, contextTone } from "./detailsmodel";
import { fmtInt, fmtUsd } from "./ui/components";
import { fmtTok } from "./ui";
import { shortModel } from "./format";
import { t } from "./i18n";

export type PanelStripProps = {
  model: string;
  /** The reasoning effort as a word, or "" when the model has none. */
  effort?: string;
  context: SessionDetail["context"] | null;
  usage: SessionDetail["usage"];
  menu: MenuItem[];
  /** The model chip opens the picker; the context and the cost open their pages in Details. */
  onModel?: () => void;
  onContext?: () => void;
  onUsage?: () => void;
};

export function PanelStrip({ model, effort, context, usage, menu, onModel, onContext, onUsage }: PanelStripProps) {
  const percent = context && context.window > 0 ? Math.round((100 * context.tokens) / context.window) : null;
  const tone = percent !== null ? contextTone(percent) : null;
  const segments = context?.breakdown ? contextSegments(context.breakdown, context.window) : null;
  const today = usage.usd_today != null;
  const todayText = stripToday(usage);
  const modelName = model || t("session.model.global");
  const contextText = context
    ? context.window > 0 ? t("strip.context.of", { used: fmtTok(context.tokens), window: fmtTok(context.window) }) : t("strip.context.used", { used: fmtTok(context.tokens) })
    : "";
  return (
    <div className="panel-strip" data-panel-strip>
      <div className="panel-strip-row">
        {onModel ? (
          <button type="button" className="panel-strip-model" onClick={onModel} title={t("strip.model.title", { model: modelName })} aria-label={t("strip.model.title", { model: modelName })}>
            <Icon name="model" size={14} />
            <span className="truncate">{shortModel(modelName, 40)}</span>
            <Icon name="chevron" size={12} />
          </button>
        ) : (
          <span className="panel-strip-model static"><Icon name="model" size={14} /><span className="truncate">{shortModel(modelName, 40)}</span></span>
        )}
        {effort && <span className="badge panel-strip-effort">{effort}</span>}
        <span className="grow" />
        {/* The chat header's list under a name of its own: two buttons both called "Session actions" on one
            screen are one name for a screen reader to tell apart by position alone. */}
        {menu.length > 0 && <OverflowMenu small label={t("strip.menu")} items={menu} />}
      </div>
      <div className="panel-strip-figures">
        {context ? (
          <button type="button" className="panel-strip-context" onClick={onContext} disabled={!onContext} data-strip-context
            title={context.window > 0 ? t("session.ctx.title", { used: fmtInt(context.tokens), window: fmtInt(context.window) }) : undefined}>
            <b className={tone ?? ""}>{percent !== null ? `${percent}%` : fmtTok(context.tokens)}</b>
            <span className="truncate">{contextText}</span>
          </button>
        ) : <span />}
        <button type="button" className="panel-strip-cost" onClick={onUsage} disabled={!onUsage} data-strip-cost title={todayText.title}>
          <b>{fmtUsd(usage.usd)}</b>
          {today && <span>{todayText.short}</span>}
        </button>
        {context && percent !== null && (
          segments ? (
            <div className="panel-strip-bar dt-segbar" role="img" aria-label={t("strip.context.bar", { n: percent })}>
              {segments.filter((part) => part.percent > 0).map((part) => <i key={part.key} data-part={part.key} style={{ width: `${part.percent}%` }} />)}
            </div>
          ) : (
            <div className={`panel-strip-bar solo ${tone ?? ""}`} role="img" aria-label={t("strip.context.bar", { n: percent })}><i style={{ width: `${Math.min(100, percent)}%` }} /></div>
          )
        )}
      </div>
    </div>
  );
}

/**
 * The day's part beside the session's total. "$13.72 · today $0" on a chat that had worked all day read
 * as a broken counter: the total came from yesterday's metered calls, and today's calls all went
 * through a subscription route, which bills nothing per call. The numbers were right; the words did
 * not say so. A day whose calls cost nothing says they were unmetered, and the title says where the
 * total comes from and that the day is UTC's.
 */
export function stripToday(usage: PanelStripProps["usage"]): { short: string; title: string } {
  const calls = usage.c_today ?? 0;
  const spent = usage.usd_today ?? 0;
  const unmetered = calls > 0 && spent === 0;
  return {
    short: unmetered ? t("strip.cost.today.unmetered") : t("strip.cost.today", { sum: fmtUsd(spent) }),
    title: t(unmetered ? "strip.cost.title.unmetered" : "strip.cost.title.day", { total: fmtUsd(usage.usd), today: fmtUsd(spent), calls }),
  };
}
