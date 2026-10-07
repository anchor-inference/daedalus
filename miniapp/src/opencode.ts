// OpenCode sells the same gateway two ways, and the difference is who pays for a call. Go is a prepaid
// monthly plan: a call spends its allowance, never money, so the host records it at $0 and no dollar cap
// counts it. Zen is pay per token at Zen's prices, capped like any priced provider. Both are the
// `opencode` kind; the provider's `billing` says which, and each has its own key proxy route.

export type Billing = "metered" | "subscription";

export type OpencodePlan = {
  id: "opencode" | "opencode_zen";
  name: string;
  billing: Billing;
  /** The key proxy route, which holds the one OpenCode key both plans take. */
  route: string;
  /** Where the plan is reached without the key proxy, with a key of the operator's own. */
  vendorUrl: string;
  /** The i18n key of its one-line description. */
  hint: string;
};

export const OPENCODE_KEY_URL = "https://opencode.ai/auth";

export const OPENCODE_PLANS: OpencodePlan[] = [
  { id: "opencode", name: "OpenCode Go", billing: "subscription", route: "opencode", vendorUrl: "https://opencode.ai/zen/go/v1", hint: "opencode.go.hint" },
  { id: "opencode_zen", name: "OpenCode Zen", billing: "metered", route: "opencode_zen", vendorUrl: "https://opencode.ai/zen/v1", hint: "opencode.zen.hint" },
];

/** Which plan an endpoint is, or null for one that is not OpenCode. A metered `opencode` endpoint is Zen. */
export function opencodePlanOf(kind: string, billing?: Billing | string): OpencodePlan | null {
  if (kind !== "opencode") return null;
  return OPENCODE_PLANS.find((plan) => plan.billing === (billing === "subscription" ? "subscription" : "metered")) ?? null;
}

/** The address a new endpoint for `plan` gets: the key proxy's route when there is one, the vendor otherwise. */
export function opencodeBaseUrl(plan: OpencodePlan, keyproxyBase: string | undefined): string {
  return keyproxyBase ? `${keyproxyBase.replace(/\/+$/, "")}/${plan.route}` : plan.vendorUrl;
}

/** The plans no configured endpoint is yet, which the app offers to add. */
export function missingPlans(providers: { kind: string; billing?: Billing | string }[]): OpencodePlan[] {
  const have = new Set(providers.map((p) => opencodePlanOf(p.kind, p.billing)?.id).filter(Boolean));
  return OPENCODE_PLANS.filter((plan) => !have.has(plan.id));
}

/** What to send to create the endpoint for `plan`. */
export function opencodeProvider(plan: OpencodePlan, keyproxyBase: string | undefined, apiKey = "") {
  return { kind: "opencode", name: plan.name, base_url: opencodeBaseUrl(plan, keyproxyBase), billing: plan.billing, ...(apiKey ? { api_key: apiKey } : {}) };
}
