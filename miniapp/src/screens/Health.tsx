// The doctor's checks as a screen of their own: what the installation found wrong with itself, the
// integrations it reaches, and the fixes it can make on its own.

import { useEffect, useState } from "react";
import { Icon, type IconName } from "../icons";
import { PageHeader, screenTitle } from "../ui/index";
import { api, telegram } from "../api";
import { plural, t } from "../i18n";
import { describeIntegration, type IntegrationRow } from "../integrationHealth";

type Check = { name: string; ok: boolean; message: string; severity: string; fix_hint: string; fixable: boolean; fixed: boolean };

function HealthTab({ toast }: { toast: (t: string) => void }) {
  const [data, setData] = useState<{ checks: Check[]; summary: Record<string, number> } | null>(null);
  const [busy, setBusy] = useState(false);
  const [integrations, setIntegrations] = useState<IntegrationRow[] | null>(null);
  const loadIntegrations = async () => {
    // The quick answer draws the card at once; the probed one follows with GitHub's verdict. Either
    // failing leaves the card as it was: the doctor's list below is the screen's real job.
    try {
      setIntegrations((await api.get<{ rows: IntegrationRow[] }>("/api/integrations/health")).rows ?? []);
      setIntegrations((await api.get<{ rows: IntegrationRow[] }>("/api/integrations/health?probe=true")).rows ?? []);
    } catch {
      /* the card stays as it was */
    }
  };
  const load = async (fix = false) => {
    setBusy(true);
    void loadIntegrations();
    try {
      setData(fix ? await api.post("/api/doctor/fix") : await api.get("/api/doctor"));
    } catch (e) {
      toast((e as Error).message);
    } finally {
      setBusy(false);
    }
  };
  useEffect(() => {
    load();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);
  if (!data) return <div className="empty">{t(busy ? "settings.health.checking" : "common.loading")}</div>;
  // The app's own glyphs, coloured by the state, as the dependency cards draw a failure; emoji drew
  // the same states in each platform's own colours and shapes.
  const mark = (c: Check): [IconName, string] => (c.fixed ? ["wrench", "ok"] : c.ok ? ["check", "ok"] : c.severity === "fail" ? ["close", "bad"] : ["alert", "warn"]);
  // An answer without `checks` is not a screen that should go down with "Cannot read properties of
  // undefined": the doctor is one endpoint away and an installation that has not got it yet reads
  // as no checks rather than as a broken page.
  const checks = data.checks ?? [];
  const summary = data.summary ?? {};
  const fixable = checks.some((c) => !c.ok && c.fixable);
  return (
    <>
      <div className="card">
        <div className="row health-summary">
          <div className="grow">
            <b>{t("settings.health.ok", { n: summary.ok ?? 0 })}</b> · {plural("settings.health.warn", summary.warn ?? 0)} · {plural("settings.health.fail", summary.fail ?? 0)}
          </div>
          <button className="btn small" disabled={busy} onClick={() => load(false)}>
            {t("settings.health.recheck")}
          </button>
          {fixable && (
            <button className="btn small primary" disabled={busy} onClick={() => load(true)}>
              {t("settings.health.fix")}
            </button>
          )}
        </div>
      </div>
      {integrations && integrations.length > 0 && <IntegrationsCard rows={integrations} />}
      <div className="card">
        {checks.map((c, i) => (
          <div key={i} className="row health-row">
            <span className={`health-mark ${mark(c)[1]}`}><Icon name={mark(c)[0]} size={16} /></span>
            <div className="grow" style={{ minWidth: 0 }}>
              <div>
                <b>{c.name}</b> <span className="sub">{c.message}</span>
              </div>
              {!c.ok && c.fix_hint && <div className="sub">→ {c.fix_hint}</div>}
            </div>
          </div>
        ))}
      </div>
    </>
  );
}

/** GitHub, each MCP server and each model provider: one line each, the remedy under a failing one,
 * and the host's technical text folded away. */
function IntegrationsCard({ rows }: { rows: IntegrationRow[] }) {
  const mark: Record<IntegrationRow["severity"], [IconName, string]> = { ok: ["check", "ok"], info: ["dot", "info"], warn: ["alert", "warn"], fail: ["close", "bad"] };
  return (
    <div className="card integrations">
      <div className="section-title">{t("health.int.title")}</div>
      {rows.map((row) => {
        const words = describeIntegration(row);
        const [icon, tone] = mark[row.severity] ?? mark.info;
        return (
          <div key={`${row.kind}:${row.name}`} className="row integration health-row" data-kind={row.kind} data-state={row.state} data-severity={row.severity}>
            <span className={`health-mark ${tone}`}><Icon name={icon} size={16} /></span>
            <div className="grow" style={{ minWidth: 0 }}>
              <div>
                <b>{row.name}</b> <span className="sub">{words.kind} · {words.state}</span>
              </div>
              {words.fix && <div className="sub integration-fix">→ {words.fix}</div>}
              {/* A healthy row's detail (a tool count, where its key comes from) is not worth a fold of its own. */}
              {row.detail && row.severity !== "ok" && (
                <details className="integration-detail">
                  <summary className="sub">{t("health.int.details")}</summary>
                  <code>{row.detail}</code>
                </details>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}

/** The doctor's checks as a screen of their own. */
export function HealthScreen({ toast }: { toast: (t: string) => void }) {
  return (
    <>
      <PageHeader title={screenTitle("health")} />
      <div className="screen narrow">
        <HealthTab toast={toast} />
        {!telegram()?.initData && (
          <div className="btnrow">
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
      </div>
    </>
  );
}
