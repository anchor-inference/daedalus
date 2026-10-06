// The Health screen's integration card: GitHub, each MCP server, each model provider. The host sends
// a state code per row (`daedalus/extensions/integration_health.py`); the words and the one-line
// remedy live here, in the operator's language, because the host's own text is technical detail
// that belongs under the fold and not in the line a person reads first.

import { t } from "./i18n";

export type IntegrationKind = "github" | "mcp" | "provider";
export type IntegrationSeverity = "ok" | "info" | "warn" | "fail";
export type IntegrationRow = { kind: IntegrationKind; name: string; state: string; severity: IntegrationSeverity; detail: string };

/** For each `kind.state`: the key of the state's words and, when there is something to do, the remedy's. */
export const INTEGRATION_WORDS: Record<string, [string, string | null]> = {
  "github.configured": ["health.int.github.configured", null],
  "github.authenticated": ["health.int.github.authenticated", null],
  "github.not_configured": ["health.int.github.not_configured", "health.int.github.not_configured.fix"],
  "github.token_rejected": ["health.int.github.token_rejected", "health.int.github.token_rejected.fix"],
  "github.unreachable": ["health.int.github.unreachable", "health.int.github.unreachable.fix"],
  "mcp.connected": ["health.int.mcp.connected", null],
  "mcp.idle": ["health.int.mcp.idle", null],
  "mcp.connecting": ["health.int.mcp.connecting", null],
  "mcp.auth_required": ["health.int.mcp.auth_required", "health.int.mcp.auth_required.fix"],
  "mcp.error": ["health.int.mcp.error", "health.int.mcp.error.fix"],
  "provider.ready": ["health.int.provider.ready", null],
  "provider.no_key": ["health.int.provider.no_key", "health.int.provider.no_key.fix"],
  "provider.key_rejected": ["health.int.provider.key_rejected", "health.int.provider.key_rejected.fix"],
  "provider.out_of_credit": ["health.int.provider.out_of_credit", "health.int.provider.out_of_credit.fix"],
  "provider.rate_limited": ["health.int.provider.rate_limited", "health.int.provider.rate_limited.fix"],
  "provider.last_call_failed": ["health.int.provider.last_call_failed", "health.int.provider.last_call_failed.fix"],
};

export const KIND_WORDS: Record<IntegrationKind, string> = {
  github: "health.int.kind.github",
  mcp: "health.int.kind.mcp",
  provider: "health.int.kind.provider",
};

/** The row in words: what kind of thing it is, its state, and what to do (empty when nothing). A
 * state this app does not know yet — a host newer than its page — reads as unknown, with the code
 * kept for the fold, rather than as a key in brackets. */
export function describeIntegration(row: IntegrationRow): { kind: string; state: string; fix: string } {
  const words = INTEGRATION_WORDS[`${row.kind}.${row.state}`];
  return {
    kind: KIND_WORDS[row.kind] ? t(KIND_WORDS[row.kind]) : row.kind,
    state: words ? t(words[0]) : t("health.int.unknown", { state: row.state }),
    fix: words?.[1] ? t(words[1]) : "",
  };
}
