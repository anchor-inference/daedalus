// A request wall around the agent's tabs. The agent's own browser sits behind a network wall the
// daemon builds; the operator's Chrome has its own network stack and cannot be put behind one. What
// can be done is narrower and still worth it: a session rule, limited to the agent's tab ids, that
// blocks requests to loopback, private ranges, link-local and metadata addresses written as literal
// addresses or `localhost`. A page the agent was steered to then cannot reach the installation's own
// ports or the operator's router by address, and the rule holds even while the debugger is detached.
// Names that resolve to such addresses are Chrome's Local Network Access prompt's business.

const RULE_ID = 1;

// RE2 syntax, matched against the whole URL. IPv6 literals appear in brackets.
export const PRIVATE_ADDRESS =
  "^[a-z][a-z0-9+.-]*://(?:[^/@]*@)?(?:localhost|127\\.\\d+\\.\\d+\\.\\d+|0\\.0\\.0\\.0|10\\.\\d+\\.\\d+\\.\\d+|" +
  "192\\.168\\.\\d+\\.\\d+|172\\.(?:1[6-9]|2\\d|3[01])\\.\\d+\\.\\d+|169\\.254\\.\\d+\\.\\d+|100\\.(?:6[4-9]|[7-9]\\d|1[01]\\d|12[0-7])\\.\\d+\\.\\d+|" +
  "\\[(?:::1|f[cd][0-9a-f:]*|fe[89ab][0-9a-f:]*|::ffff:[0-9a-f.:]*)\\])(?::\\d+)?(?:[/?#]|$)";

export async function updateGuard(tabIds: number[]): Promise<void> {
  // A rule with an empty `tabIds` would match no tab at all, but Chrome refuses an empty list, so the
  // rule is removed instead while the agent has no tabs.
  const addRules: chrome.declarativeNetRequest.Rule[] = tabIds.length
    ? [{ id: RULE_ID, priority: 1, action: { type: "block" }, condition: { regexFilter: PRIVATE_ADDRESS, tabIds } }]
    : [];
  try {
    await chrome.declarativeNetRequest.updateSessionRules({ removeRuleIds: [RULE_ID], addRules });
  } catch {
    // A refused rule must not take the relay down with it; the daemon's own guard still stands.
  }
}
