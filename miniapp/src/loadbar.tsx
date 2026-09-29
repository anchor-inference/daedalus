// The load bar: what running terminals cost the machine now, and what it would carry with the cap
// filled. Settings shows it under the cap while the operator edits it; the Terminals screen can show
// the same component in its header with the configured cap.
//
// One track is the machine's memory. From the left: what everything else uses, what the terminals use
// now, and — lighter — what the terminals up to the cap would add. The colour is the projection's, on
// the app's own ok/warn/bad tokens, and the sentences under it say what each number is, because a bar
// alone reads as "a percentage of something".

import type { TerminalLoad, WorkloadsLoad } from "./api";
import { plural, t } from "./i18n";
import { loadFigures, overEstimate, roundBytes, workloadFigures } from "./machineload";

/** A size as the sentences use it: "14 GB", "820 MB". */
export function size(n: number): string {
  const { value, unit } = roundBytes(n);
  return t(`fmt.bytes.${unit}`, { n: value });
}

const pct = (n: number) => Math.round(n);

/** A share of the track, as a CSS width that never runs past what is left of it. */
function share(part: number, total: number, used: number): number {
  if (!total || part <= 0) return 0;
  return Math.max(0, Math.min(100 - used, (100 * part) / total));
}

export function LoadBar({ load, cap, compact = false }: { load: TerminalLoad; cap?: number; compact?: boolean }) {
  const f = loadFigures(load, cap ?? load.cap);
  if (!f.known) {
    return (
      <div className="loadbar" data-level="unknown">
        <div className="sub">{t("load.unknown")}</div>
      </div>
    );
  }
  const other = Math.max(0, f.machineNow - f.terminalsNow);
  const otherW = share(other, f.total, 0);
  const nowW = share(f.terminalsNow, f.total, otherW);
  const extraW = share(f.machineAtCap - f.machineNow, f.total, otherW + nowW);
  const daemon = load.used.daemon_rss_bytes ?? 0;
  return (
    <div className="loadbar" data-level={f.level}>
      <div
        className="loadbar-track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct(f.memPercentAtCap)}
        aria-label={t("load.aria", { percent: pct(f.memPercentAtCap), n: f.cap })}
      >
        <i className="loadbar-other" style={{ width: `${otherW}%` }} />
        <i className="loadbar-now" style={{ width: `${nowW}%` }} />
        <i className="loadbar-extra" style={{ width: `${extraW}%` }} />
      </div>
      <div className="loadbar-head">
        <b>{plural("load.atcap", f.cap, { used: size(f.terminalsAtCap), total: size(f.total) })}</b>
        <span className="loadbar-percent">{t("load.percent", { percent: pct(f.memPercentAtCap) })}</span>
      </div>
      {/* What each fill of the track is, in its own colour and pattern: the solid and the striped
          part had no key, and nobody could tell the terminals running now from the room the limit
          keeps for them. */}
      <div className="loadbar-legend">
        <span className="loadbar-key"><i className="loadbar-other" />{t("load.legend.other", { used: size(other) })}</span>
        <span className="loadbar-key" title={daemon > 0 ? t("load.daemon", { used: size(daemon) }) : undefined}><i className="loadbar-now" />{plural("load.legend.now", f.running, { used: size(f.terminalsNow) })}</span>
        {f.extra > 0 && <span className="loadbar-key"><i className="loadbar-extra" />{t("load.legend.extra", { cap: f.cap, used: size(f.machineAtCap - f.machineNow) })}</span>}
      </div>
      {compact ? (
        // The page's header keeps the facts short and named — free memory, the processors — where two
        // sentences of figures used to make a newcomer parse prose for which number was now.
        <div className="sub loadbar-cpu" data-level={f.cpuLevel}>
          {t("load.stats.free", { free: size(Math.max(0, f.total - f.machineNow)) })} · {t("load.stats.cpu", { now: pct(f.cpuNow), atcap: pct(f.cpuAtCap), cap: f.cap })}
        </div>
      ) : (
        <>
          <div className="sub">
            {plural("load.now", f.running, { used: size(f.terminalsNow), free: size(Math.max(0, f.total - f.machineNow)) })}
            {daemon > 0 && ` ${t("load.daemon", { used: size(daemon) })}`}
          </div>
          <div className="sub">{t("load.machine", { now: pct(f.memPercentNow), atcap: pct(f.memPercentAtCap) })}</div>
          <div className="sub loadbar-cpu" data-level={f.cpuLevel}>{t("load.cpu", { now: pct(f.cpuNow), atcap: pct(f.cpuAtCap) })}</div>
        </>
      )}
      {!compact && <div className="sub faint">{t(`load.basis.${load.likely.basis}`, { each: size(load.likely.rss_bytes) })}</div>}
      {overEstimate(f) && (
        <div className="loadbar-warning" role="status">
          {f.level !== "bad" ? t("load.over.cpu") : f.supported !== null ? plural("load.over", f.supported) : t("load.over.memory")}
        </div>
      )}
    </div>
  );
}

/**
 * Terminals and browsers on the one memory track the machine has: what everything else uses, the
 * terminals now, the browsers now, and — striped — what each would add filled to its own cap. The
 * colour and the warning are the two together's, since both fill the same memory; the sentences name
 * each. Where the installation has only one kind, it is that kind's own bar.
 */
export function WorkloadsBar({ load, caps = {} }: { load: WorkloadsLoad; caps?: { terminals?: number; browsers?: number } }) {
  if (!load.browsers && load.terminals) return <LoadBar load={load.terminals} cap={caps.terminals} />;
  const f = workloadFigures(load.terminals, load.browsers, caps);
  if (!f.known) {
    return (
      <div className="loadbar" data-level="unknown">
        <div className="sub">{t("load.unknown.any")}</div>
      </div>
    );
  }
  const tNow = f.terminals?.now ?? 0;
  const bNow = f.browsers?.now ?? 0;
  const other = Math.max(0, f.machineNow - tNow - bNow);
  let used = 0;
  const w = (part: number) => {
    const v = share(part, f.total, used);
    used += v;
    return v;
  };
  const otherW = w(other);
  const tNowW = w(tNow);
  const bNowW = w(bNow);
  const tExtraW = w(f.terminals?.extra ?? 0);
  const bExtraW = w(f.browsers?.extra ?? 0);
  const over = f.level === "bad" || f.cpuLevel === "bad";
  return (
    <div className="loadbar workloads" data-level={f.level}>
      <div
        className="loadbar-track"
        role="meter"
        aria-valuemin={0}
        aria-valuemax={100}
        aria-valuenow={pct(f.memPercentAtCap)}
        aria-label={t("load.together.aria", { percent: pct(f.memPercentAtCap) })}
      >
        <i className="loadbar-other" style={{ width: `${otherW}%` }} />
        <i className="loadbar-now" style={{ width: `${tNowW}%` }} />
        <i className="loadbar-browsers" style={{ width: `${bNowW}%` }} />
        <i className="loadbar-extra" style={{ width: `${tExtraW}%` }} />
        <i className="loadbar-browsers-extra" style={{ width: `${bExtraW}%` }} />
      </div>
      <div className="loadbar-head">
        <b>{t("load.together", { used: size(f.machineAtCap), total: size(f.total) })}</b>
        <span className="loadbar-percent">{t("load.percent", { percent: pct(f.memPercentAtCap) })}</span>
      </div>
      {f.terminals && (
        <div className="sub loadbar-kind terminals">
          <i aria-hidden="true" />
          {plural("load.kind.terminals", f.terminals.running, { used: size(f.terminals.now), cap: f.terminals.cap, atcap: size(f.terminals.atCap) })}
        </div>
      )}
      {f.browsers && (
        <div className="sub loadbar-kind browsers">
          <i aria-hidden="true" />
          {plural("load.kind.browsers", f.browsers.running, { used: size(f.browsers.now), cap: f.browsers.cap, atcap: size(f.browsers.atCap) })}
        </div>
      )}
      <div className="sub">{t("load.machine", { now: pct(f.memPercentNow), atcap: pct(f.memPercentAtCap) })}</div>
      <div className="sub loadbar-cpu" data-level={f.cpuLevel}>{t("load.cpu", { now: pct(f.cpuNow), atcap: pct(f.cpuAtCap) })}</div>
      {over && (
        <div className="loadbar-warning" role="status">
          {f.level === "bad" ? t("load.over.together") : t("load.over.cpu")}
        </div>
      )}
    </div>
  );
}
