// Adding an MCP server in one step: paste the command a README gives (`npx -y @x/y-mcp`), its
// https:// address, or its JSON block; the host starts it once, unsaved, and shows what came of it —
// its state, where it ran, the last of what it wrote to stderr, and its tools — before anything is
// kept. Adding it then saves it with the policy chosen here, and in a session switches it on there.
// Before this the operator filled a form field by field, and a server that did not start said only
// "Connection closed": why it failed went to the container's log.

import { useState } from "react";
import { api } from "./api";
import { t } from "./i18n";
import { errorText } from "./ui";
import { Segmented } from "./ui/components";

export type McpPolicy = "auto" | "ask" | "allow" | "deny";
type TrialTool = { name: string; description: string; approval: string };
type Trial = { name: string; state: string; error: string | null; tools: TrialTool[]; stderr: string; runs_in: string; exists: boolean };

const TONE: Record<string, string> = { ready: "ok", connecting: "warn", auth_required: "warn", unavailable: "bad" };

export function policyOptions(): { id: McpPolicy; label: string }[] {
  return [
    { id: "auto", label: t("mcp.policy.auto") },
    { id: "ask", label: t("mcp.policy.ask") },
    { id: "allow", label: t("mcp.policy.allow") },
    { id: "deny", label: t("mcp.policy.deny") },
  ];
}

export function McpQuickAdd({ sessionId, toast, onAdded }: { sessionId?: string; toast: (text: string) => void; onAdded: (servers?: unknown) => void }) {
  const [input, setInput] = useState("");
  const [trial, setTrial] = useState<Trial | null>(null);
  const [name, setName] = useState("");
  const [policy, setPolicy] = useState<McpPolicy>("auto");
  const [busy, setBusy] = useState<"try" | "add" | null>(null);

  async function tryIt() {
    setBusy("try");
    setTrial(null);
    try {
      const result = await api.post<Trial>("/api/mcp/try", { input: input.trim() });
      setTrial(result);
      setName(result.name);
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }
  async function add() {
    setBusy("add");
    try {
      const answer = await api.post<{ name: string; probe: { state: string; error: string | null }; enabled: string[] | null; servers: unknown }>(
        "/api/mcp/add", { input: input.trim(), name: name.trim(), approval: policy, session_id: sessionId ?? "" });
      toast(answer.enabled ? t("mcp.quick.added.enabled", { name: answer.name }) : t("mcp.quick.added", { name: answer.name }));
      setTrial(null);
      setInput("");
      onAdded(answer.servers);
    } catch (error) {
      toast(errorText(error));
    } finally { setBusy(null); }
  }

  return (
    <div className="mcp-quick" data-mcp-quick>
      <div className="mcp-quick-row">
        <input className="field mono grow" value={input} spellCheck={false} placeholder={t("mcp.quick.placeholder")} aria-label={t("mcp.quick.label")}
          onChange={(e) => { setInput(e.target.value); setTrial(null); }}
          onKeyDown={(e) => { if (e.key === "Enter" && input.trim() && !busy) void tryIt(); }} />
        <button type="button" className="btn small" disabled={!input.trim() || !!busy} onClick={() => void tryIt()}>
          {busy === "try" ? t("mcp.quick.trying") : t("mcp.quick.try")}
        </button>
      </div>
      {busy === "try" && <div className="dt-hint">{t("mcp.quick.wait")}</div>}
      {trial && (
        <div className="dt-server mcp-trial" data-mcp-trial={trial.state}>
          <div className="dt-server-head">
            <input className="field mono mcp-trial-name" value={name} aria-label={t("settings.mcp.name")} spellCheck={false} onChange={(e) => setName(e.target.value)} />
            <span className="dt-state" data-tone={TONE[trial.state] ?? ""}>{t(`session.mcp.state.${trial.state}`)}</span>
          </div>
          {trial.runs_in && <div className="dt-hint" data-runs-in>{t(`mcp.where.${trial.runs_in}`)}</div>}
          {trial.error && <div className="dt-hint" data-tone="bad">{trial.error}</div>}
          {trial.stderr && (
            <details className="mcp-stderr" open={trial.state !== "ready"}>
              <summary>{t("mcp.quick.stderr")}</summary>
              <pre className="mono">{trial.stderr}</pre>
            </details>
          )}
          {trial.tools.length > 0 && (
            <div className="dt-tags" data-trial-tools>
              {trial.tools.map((tool) => <span key={tool.name} className="dt-tag mono" title={tool.description} data-approval={tool.approval}>{tool.name}</span>)}
            </div>
          )}
          <div className="mfield wide">
            <span>{t("mcp.policy")}</span>
            <Segmented value={policy} label={t("mcp.policy")} onChange={setPolicy} options={policyOptions()} />
          </div>
          {trial.exists && <div className="dt-hint" data-tone="warn">{t("mcp.quick.exists", { name })}</div>}
          <div className="mcp-actions">
            <button type="button" className="btn ghost small" onClick={() => setTrial(null)}>{t("common.cancel")}</button>
            <button type="button" className="btn primary small" disabled={!!busy || !name.trim()} onClick={() => void add()}>
              {busy === "add" ? t("settings.mcp.saving") : sessionId ? t("mcp.quick.addHere") : t("settings.mcp.add")}
            </button>
          </div>
        </div>
      )}
    </div>
  );
}
