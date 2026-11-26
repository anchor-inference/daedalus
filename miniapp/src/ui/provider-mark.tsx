import type { ReactNode } from "react";
import type { ProviderConf, Settings } from "../api";
import type { SubscriptionUpstream } from "../accounts";
import { t } from "../i18n";
import { ICON_STROKE } from "../icons";

const NAMES: Record<string, string> = {
  anthropic:"Anthropic", claude:"Anthropic", openai:"OpenAI", deepseek:"DeepSeek",
  codex:"OpenAI / Codex", grok:"xAI", xai:"xAI", opencode:"OpenCode Go", openrouter:"OpenRouter", zai:"Z.ai", zhipu:"Z.ai",
  google:"Google", gemini:"Google", antigravity:"Google", vllm:"vLLM", llamacpp:"llama.cpp", ollama:"Ollama",
};

/** The families with a mark of their own; every other endpoint is a letter tile when named. */
const BRANDS = new Set(["anthropic", "claude", "openai", "codex", "deepseek", "google", "gemini", "antigravity", "openrouter", "opencode", "zai", "zhipu", "grok", "xai"]);

function family(id: string, kind?: string): string {
  const key = id.toLowerCase().replace(/[^a-z0-9]/g, "");
  const known = Object.keys(NAMES).find((name) => key === name || key.startsWith(name));
  return known ?? (kind && kind !== "openai" ? kind : "endpoint");
}

/** Anthropic is reached two ways, and both were called plain "Anthropic": the picker listed two rows of
 *  one name, one the `claude` subscription login and one the `anthropic` key. They say which they are. */
const WORDED: Record<string, string> = { claude: "provider.name.claude", anthropic: "provider.name.anthropic" };

export function providerName(id: string, conf?: ProviderConf): string {
  const key = id.toLowerCase();
  return conf?.name || (WORDED[key] ? t(WORDED[key]) : NAMES[key]) || id;
}

/**
 * The tile's letters: the first letters of the first two words of the name the operator sees ("WT" for
 * Work-test-vllm), or the first two of a name of one word ("AB" for abliteration). One letter was not
 * enough: "test-a" and "test-b", or two local servers, were the same tile twice.
 */
export function monogram(name: string): string {
  // Words split at anything that is not a letter or a digit, at a lower-to-upper step ("myVllm") and
  // where letters meet digits ("test1"), so a numbered endpoint keeps its number.
  const words = name.replace(/([\p{Ll}])([\p{Lu}])/gu, "$1 $2").replace(/([\p{L}])([\p{N}])|([\p{N}])([\p{L}])/gu, "$1$3 $2$4")
    .split(/[^\p{L}\p{N}]+/u).filter(Boolean);
  if (!words.length) return "?";
  const letters = words.length > 1 ? [...words[0]][0] + [...words[1]][0] : [...words[0]].slice(0, 2).join("");
  return letters.toLocaleUpperCase();
}

const ANTHROPIC = <path d="M5 18L10 6l5 12M7 14h6M17 6l3 12" />;
const OPENAI = <><path d="M12 3l8 4v10l-8 4-8-4V7zM12 3v7l8 7M4 7l8 3v11M4 17l8-7 8-3" /><circle cx="12" cy="10" r="2" /></>;
const GOOGLE = <path d="M12 3c0 5 4 9 9 9-5 0-9 4-9 9 0-5-4-9-9-9 5 0 9-4 9-9z" />;
const ZAI = <path d="M5 6h14L5 18h14" />;
const XAI = <path d="M5 5l14 14M19 5L5 19M7 3h6M11 21h6" />;
const SERVER = <><rect x="4" y="5" width="16" height="14" rx="3" /><path d="M8 9l3 3-3 3M13 15h3" /></>;

/** Every mark of the app that stands for a service rather than for an action, drawn by one function:
 *  the model providers, and (under a `cal-` prefix, because Google is both) the calendar services of
 *  Settings → Calendars. Lucide carries no brand marks, so these are the app's own monochrome strokes,
 *  in the same weight as the icon set; a family with no drawing gets the crosshair. */
const DRAWINGS: Record<string, ReactNode> = {
  anthropic: ANTHROPIC, claude: ANTHROPIC,
  deepseek: <path d="M4 12c1-5 7-7 12-3l4-3v6c0 5-5 8-10 6-3-1-5-3-6-6zM8 11h.01M11 7l-1-3M13 17l-2 3" />,
  openai: OPENAI, codex: OPENAI,
  google: GOOGLE, gemini: GOOGLE, antigravity: GOOGLE,
  openrouter: <path d="M4 12h6l8-7M10 12l8 7M14 5h4v4M14 19h4v-4" />,
  opencode: <path d="M9 6l-6 6 6 6M15 6l6 6-6 6M14 4l-4 16" />,
  zai: ZAI, zhipu: ZAI,
  grok: XAI, xai: XAI,
  vllm: SERVER, llamacpp: SERVER, ollama: SERVER,
  "cal-google": <path d="M20 12.2c0 4.6-3.2 7.8-8 7.8a8 8 0 1 1 5.4-13.9M20 12h-8" />,
  "cal-outlook": <><rect x="3" y="6" width="11" height="12" rx="2" /><circle cx="8.5" cy="12" r="2.5" /><path d="M14 8h7v8h-7M14 9l3.5 3L21 9" /></>,
  "cal-yandex": <path d="M14 20V4h-1.6C9.6 4 8 5.6 8 8.2s1.4 4 4 4.6M12 12.8L7.5 20" />,
  "cal-icloud": <path d="M7 18h10a4 4 0 0 0 .5-8 6 6 0 0 0-11.4 1.4A3.3 3.3 0 0 0 7 18z" />,
  "cal-ics": <><path d="M5 11a8 8 0 0 1 8 8M5 5a14 14 0 0 1 14 14" /><circle cx="6" cy="18" r="1.5" /></>,
  "cal-caldav": <><rect x="4" y="5" width="16" height="15" rx="2" /><path d="M4 10h16M9 3v4M15 3v4M9 15l2 2 4-4" /></>,
};

const GENERIC = <><circle cx="12" cy="12" r="7" /><path d="M12 5v14M5 12h14M7 7l10 10M7 17L17 7" /></>;

export function FamilyGlyph({ family, size = 20 }: { family: string; size?: number }) {
  return (
    <svg viewBox="0 0 24 24" width={size} height={size} fill="none" stroke="currentColor" strokeWidth={ICON_STROKE} strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      {DRAWINGS[family] ?? GENERIC}
    </svg>
  );
}

/**
 * A provider's mark: the brand's monochrome stroke for a family that has one, and for every other
 * endpoint (a vLLM or llama.cpp server, any custom address) a neutral tile with the letters of its
 * name. Those endpoints used to share one terminal or crosshair glyph, and a list of fourteen
 * providers showed four identical pictures side by side. `name` defaults to the provider's own.
 */
export function ProviderMark({ id, kind, name, className = "" }: { id: string; kind?: string; name?: string; className?: string }) {
  const key = family(id, kind);
  if (["vllm", "llamacpp", "ollama"].includes(key) || !BRANDS.has(key)) {
    const letters = monogram(name ?? providerName(id));
    return <span className={`provider-mark provider-letter ${className}`.trim()} data-letters={letters.length} aria-hidden="true"><span>{letters}</span></span>;
  }
  return <span className={`provider-mark provider-${key} ${className}`.trim()} aria-hidden="true">
    <FamilyGlyph family={key} />
  </span>;
}

export type ProviderState = { word: string; tone: "live" | "attention" | "idle" };

/** Whether a provider can answer, in the word Settings → Models and providers shows beside it and the
 *  model picker beside its name: connected, no key, needs sign-in, not answering. */
export function providerState(conf: ProviderConf | undefined, s: Pick<Settings, "provider_keys">, id: string, accounts?: SubscriptionUpstream): ProviderState {
  if (!conf) return { word: t("prov.status.missing"), tone: "attention" };
  const keys = s.provider_keys?.[id];
  const signedOut = accounts && accounts.sign_in === "terminal" && accounts.accounts.length > 0 && !accounts.accounts.some((a) => a.configured);
  if (signedOut) return { word: t("prov.status.signin"), tone: "attention" };
  if (keys?.ready) return { word: t("prov.status.ready"), tone: "live" };
  if (!keys) return conf.api_key_set ? { word: t("settings.provider.keystored"), tone: "idle" } : { word: t("prov.status.unknown"), tone: "idle" };
  if (keys.key_held === null) return { word: t("prov.status.proxy"), tone: "attention" };
  if (keys.key_kind === "cli_login") return { word: t("prov.status.signin"), tone: "attention" };
  if (keys.key_kind === "endpoint") return { word: t("prov.status.down"), tone: "attention" };
  return { word: t("prov.status.nokey"), tone: "attention" };
}
