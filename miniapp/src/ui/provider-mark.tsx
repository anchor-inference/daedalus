import type { ReactNode } from "react";
import type { ProviderConf } from "../api";
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

/** The tile's letter: the first letter or digit of the name the operator sees. */
function initial(name: string): string {
  return (/[\p{L}\p{N}]/u.exec(name)?.[0] ?? "?").toLocaleUpperCase();
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
 * Local vector marks distinguish endpoint families without fetching third-party assets.
 *
 * Given the provider's display `name`, an endpoint with no brand mark of its own (a vLLM or llama.cpp
 * server, any custom address) is drawn as a tile with the name's initial instead of the shared
 * terminal or crosshair glyph: on a phone list two such endpoints side by side ("vLLM",
 * "Work-test-vllm") were otherwise one picture twice. The desktop picker passes no name and keeps
 * its glyphs.
 */
export function ProviderMark({ id, kind, name, className = "" }: { id: string; kind?: string; name?: string; className?: string }) {
  const key = family(id, kind);
  const local = ["vllm", "llamacpp", "ollama"].includes(key);
  if (name && (local || !BRANDS.has(key))) {
    return <span className={`provider-mark provider-letter ${className}`} aria-hidden="true">{initial(name)}</span>;
  }
  return <span className={`provider-mark provider-${key} ${className}`.trim()} aria-hidden="true">
    <FamilyGlyph family={key} />
  </span>;
}
