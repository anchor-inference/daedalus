// Which DevTools methods the daemon may run through this extension. The daemon has its own rules;
// this list is the second wall, so that a daemon or a bridge that has been taken over still cannot
// read the operator's cookies, clear their storage or reach past the tabs the agent was given. The
// scope half of that (the tab must be in an agent group) is checked by the relay; this file judges
// the method alone.

const DOMAINS = new Set([
  "Page",
  "Runtime",
  "DOM",
  "DOMDebugger",
  "DOMSnapshot",
  "Input",
  "Accessibility",
  "Log",
  "Fetch",
  "Network",
]);

// Emulation is allowed for focus, media and device metrics only. The rest of the domain changes how
// the operator's own browser presents itself (user agent, geolocation, time zone, CPU throttling),
// and the daemon must not do that to a profile that is not its own.
const EMULATION = new Set([
  "Emulation.setFocusEmulationEnabled",
  "Emulation.setEmulatedMedia",
  "Emulation.setDeviceMetricsOverride",
  "Emulation.clearDeviceMetricsOverride",
]);

// Target is allowed only to follow a tab's own children (frames of another site, workers) through
// flat sessions, and to let go of one. Creating, listing or attaching to arbitrary targets is what
// the browser-level session does, and the extension has none to give.
const TARGET = new Set(["Target.setAutoAttach", "Target.detachFromTarget"]);

// Refused by name even though their domain is allowed. Anything mentioning a cookie is refused as
// well, whatever domain it is in, so that a method Chrome adds later (`Network.setCookieControls`,
// `Page.getCookies` of old) is closed before anyone has to remember to close it.
const REFUSED = new Set(["Network.clearBrowserCache"]);

/** Why a method is refused, or null when it may run on one of the agent's own tabs. */
export function refusal(method: string): string | null {
  const dot = method.indexOf(".");
  if (dot <= 0 || dot === method.length - 1) return "not a DevTools method";
  const domain = method.slice(0, dot);
  if (/cookie/i.test(method)) return "cookies are never reachable through the extension";
  if (REFUSED.has(method)) return `${method} is refused`;
  if (domain === "Storage") return "the Storage domain is refused";
  if (domain === "Browser") return "the Browser domain is refused";
  if (domain === "Emulation") return EMULATION.has(method) ? null : `${method} is refused`;
  if (domain === "Target") return TARGET.has(method) ? null : `${method} is refused`;
  return DOMAINS.has(domain) ? null : `the ${domain} domain is not on the extension's allowlist`;
}
