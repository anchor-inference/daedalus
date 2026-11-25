// The doctor's checks as a screen of their own: what the installation found wrong with itself, the
// integrations it reaches, and the fixes it can make on its own. What needs the operator comes first
// and stands open, with its remedy; everything that works is a quiet row under it.

import { useEffect, useState } from "react";
import { Icon } from "../icons";
import { PageHeader, screenTitle, useMedia } from "../ui/index";
import { Page } from "../shell";
import { Button, Dot, IconButton } from "../ui/primitives";
import { api, telegram } from "../api";
import { plural, t } from "../i18n";
import { describeIntegration, type IntegrationRow } from "../integrationHealth";

type Check = { name: string; ok: boolean; message: string; severity: string; fix_hint: string; fixable: boolean; fixed: boolean };
type Tone = "live" | "attention" | "idle";

/** One line of the page, whichever list it came from: a doctor's check or an integration. */
type Line = {
  key: string;
  name: string;
  text: string;
  tone: Tone;
  word: string;
  attention: boolean;
  /** What to do about it, in a sentence, when there is something to do. */
  remedy: string;
  /** The host's own text, which is technical and so stays inside the open row. */
  detail: string;
  fixable: boolean;
  integration?: IntegrationRow;
};

function checkLine(c: Check, i: number): Line {
  const bad = !c.ok && !c.fixed;
  return {
    key: `check:${i}:${c.name}`,
    name: c.name,
    text: c.message,
    tone: bad ? "attention" : "live",
    word: t(c.fixed ? "health.state.fixed" : c.ok ? "health.state.ok" : c.severity === "fail" ? "health.state.fail" : "health.state.warn"),
    attention: bad,
    remedy: !c.ok ? c.fix_hint : "",
    detail: "",
    fixable: !c.ok && c.fixable,
  };
}

function integrationLine(row: IntegrationRow): Line {
  const words = describeIntegration(row);
  const bad = row.severity === "warn" || row.severity === "fail";
  return {
    key: `${row.kind}:${row.name}`,
    name: row.name,
    text: `${words.kind} · ${words.state}`,
    tone: bad ? "attention" : row.severity === "ok" ? "live" : "idle",
    word: t(`health.state.${row.severity === "info" || row.severity === "ok" || row.severity === "warn" || row.severity === "fail" ? row.severity : "info"}`),
    attention: bad,
    remedy: words.fix,
    // A healthy row's detail (a tool count, where its key comes from) is not worth opening a row for.
    detail: row.severity === "ok" ? "" : row.detail,
    fixable: false,
    integration: row,
  };
}

function HealthRow({ line, open, onToggle, onFix, busy }: { line: Line; open: boolean; onToggle: () => void; onFix: () => void; busy: boolean }) {
  const expandable = !!(line.remedy || line.detail || line.fixable);
  const integration = line.integration;
  const head = (
    <>
      <span className={`pg-state ${line.tone}`}><Dot tone={line.tone} />{line.word}</span>
      <span className="health-name">{line.name}</span>
      <span className="health-text">{line.text}</span>
      {expandable && <Icon name="chevron" size={14} />}
    </>
  );
  return (
    <div className={`health-row ${open ? "open" : ""} ${integration ? "integration" : ""}`} data-state={integration?.state} data-kind={integration?.kind} data-severity={integration?.severity} data-attention={line.attention}>
      {expandable ? (
        <button type="button" className="health-head" aria-expanded={open} onClick={onToggle}>{head}</button>
      ) : (
        <div className="health-head">{head}</div>
      )}
      {open && expandable && (
        <div className="health-detail">
          {line.remedy && <div className={integration ? "integration-fix" : "health-hint"}>{integration ? `→ ${line.remedy}` : line.remedy}</div>}
          {line.detail && <code className="health-code">{line.detail}</code>}
          {line.fixable && <div><Button size="sm" variant="primary" disabled={busy} onClick={onFix}>{t("settings.health.fix")}</Button></div>}
        </div>
      )}
    </div>
  );
}

/** The doctor's checks as a screen of their own. */
export function HealthScreen({ toast }: { toast: (t: string) => void }) {
  const [data, setData] = useState<{ checks: Check[]; summary: Record<string, number> } | null>(null);
  const phone = !useMedia("(min-width: 1024px)");
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [integrations, setIntegrations] = useState<IntegrationRow[] | null>(null);
  // The rows the operator turned over: a row that needs them starts open and closes here, a quiet
  // one starts closed and opens here.
  const [turned, setTurned] = useState<Set<string>>(new Set());
  const loadIntegrations = async () => {
    // The quick answer draws the rows at once; the probed one follows with GitHub's verdict. Either
    // failing leaves them as they were: the doctor's list is the screen's real job.
    try {
      setIntegrations((await api.get<{ rows: IntegrationRow[] }>("/api/integrations/health")).rows ?? []);
      setIntegrations((await api.get<{ rows: IntegrationRow[] }>("/api/integrations/health?probe=true")).rows ?? []);
    } catch {
      /* the rows stay as they were */
    }
  };
  const load = async (fix = false) => {
    setBusy(true);
    setFailed(false);
    void loadIntegrations();
    try {
      setData(fix ? await api.post("/api/doctor/fix") : await api.get("/api/doctor"));
    } catch (e) {
      setFailed(true);
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // An answer without `checks` is not a screen that should go down with "Cannot read properties of
  // undefined": the doctor is one endpoint away and an installation that has not got it yet reads
  // as no checks rather than as a broken page.
  const checks = (data?.checks ?? []).map(checkLine);
  const integrationLines = (integrations ?? []).map(integrationLine);
  const all = [...checks, ...integrationLines];
  const attention = all.filter((l) => l.attention);
  const fixable = (data?.checks ?? []).some((c) => !c.ok && c.fixable);
  const warn = attention.filter((l) => l.word === t("health.state.warn")).length;
  const subtitle = data ? `${t("settings.health.ok", { n: all.length - attention.length })} · ${plural("settings.health.warn", warn)} · ${plural("settings.health.fail", attention.length - warn)}` : undefined;
  const isOpen = (line: Line) => (line.attention ? !turned.has(line.key) : turned.has(line.key));
  const toggle = (key: string) => setTurned((s) => { const next = new Set(s); if (!next.delete(key)) next.add(key); return next; });
  const rows = (lines: Line[]) => (
    <div className="pg-card">
      {lines.map((line) => <HealthRow key={line.key} line={line} open={isOpen(line)} onToggle={() => toggle(line.key)} onFix={() => void load(true)} busy={busy} />)}
    </div>
  );
  const quietIntegrations = integrationLines.filter((l) => !l.attention);
  const quietChecks = checks.filter((l) => !l.attention);
  return (
    <>
      <PageHeader
        title={screenTitle("health")}
        subtitle={subtitle}
        actions={
          // A phone's bar has the room of a title and two glyphs, not of two labelled buttons.
          phone ? (
            <>
              <IconButton icon="reload" label={t("settings.health.recheck")} disabled={busy} onClick={() => void load(false)} />
              {fixable && <IconButton className="primary" icon="wrench" label={t("settings.health.fix")} disabled={busy} onClick={() => void load(true)} />}
            </>
          ) : (
            <>
              <Button icon="reload" disabled={busy} onClick={() => void load(false)}>{t("settings.health.recheck")}</Button>
              {fixable && <Button variant="primary" icon="wrench" disabled={busy} onClick={() => void load(true)}>{t("settings.health.fix")}</Button>}
            </>
          )
        }
      />
      <Page width="list" className="health">
        {!data && !failed && <div className="empty">{t(busy ? "settings.health.checking" : "common.loading")}</div>}
        {!data && failed && (
          <div className="empty">
            <b>{t("health.error")}</b>
            <div>{t("health.error.sub")}</div>
            <button className="btn primary" onClick={() => void load(false)}>{t("common.retry")}</button>
          </div>
        )}
        {data && (
          <>
            {attention.length > 0 && (
              <>
                <div className="pg-label attention"><span>{t("health.attention")}</span><span className="pg-label-note">{attention.length}</span></div>
                {rows(attention)}
              </>
            )}
            {quietIntegrations.length > 0 && (
              <>
                <div className="pg-label"><span>{t("health.int.title")}</span></div>
                <div className="integrations">{rows(quietIntegrations)}</div>
              </>
            )}
            {quietChecks.length > 0 && (
              <>
                <div className="pg-label"><span>{t("health.checks")}</span></div>
                {rows(quietChecks)}
              </>
            )}
            {!telegram()?.initData && (
              <div className="btnrow health-foot">
                <button
                  className="btn small"
                  onClick={async () => {
                    try {
                      await api.post("/api/auth/logout");
                      sessionStorage.removeItem("daedalus_token");
                    } finally {
                      window.location.reload();
                    }
                  }}
                >
                  {t("settings.logout")}
                </button>
              </div>
            )}
          </>
        )}
      </Page>
    </>
  );
}
