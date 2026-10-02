import type { ProviderConf } from "../api";

const NAMES: Record<string, string> = {
  anthropic:"Anthropic", claude:"Anthropic", openai:"OpenAI", deepseek:"DeepSeek",
  codex:"OpenAI / Codex", grok:"xAI", xai:"xAI", opencode:"OpenCode Go", openrouter:"OpenRouter", zai:"Z.ai", zhipu:"Z.ai",
  google:"Google", gemini:"Google", vllm:"vLLM", llamacpp:"llama.cpp", ollama:"Ollama",
};

function family(id: string, kind?: string): string {
  const key = id.toLowerCase().replace(/[^a-z0-9]/g, "");
  const known = Object.keys(NAMES).find((name) => key === name || key.startsWith(name));
  return known ?? (kind && kind !== "openai" ? kind : "endpoint");
}

export function providerName(id: string, conf?: ProviderConf): string {
  return conf?.name || NAMES[id.toLowerCase()] || id;
}

/** Local vector marks distinguish endpoint families without fetching third-party assets. */
export function ProviderMark({ id, kind }: { id: string; kind?: string }) {
  const key = family(id, kind);
  const local = ["vllm", "llamacpp", "ollama"].includes(key);
  return <span className={`provider-mark provider-${key}`} aria-hidden="true">
    <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round">
      {key === "anthropic" || key === "claude" ? <path d="M5 18L10 6l5 12M7 14h6M17 6l3 12" />
        : key === "deepseek" ? <path d="M4 12c1-5 7-7 12-3l4-3v6c0 5-5 8-10 6-3-1-5-3-6-6zM8 11h.01M11 7l-1-3M13 17l-2 3" />
        : key === "openai" || key === "codex" ? <><path d="M12 3l8 4v10l-8 4-8-4V7zM12 3v7l8 7M4 7l8 3v11M4 17l8-7 8-3" /><circle cx="12" cy="10" r="2" /></>
        : key === "google" || key === "gemini" ? <path d="M12 3c0 5 4 9 9 9-5 0-9 4-9 9 0-5-4-9-9-9 5 0 9-4 9-9z" />
        : key === "openrouter" ? <path d="M4 12h6l8-7M10 12l8 7M14 5h4v4M14 19h4v-4" />
        : key === "opencode" ? <path d="M9 6l-6 6 6 6M15 6l6 6-6 6M14 4l-4 16" />
        : key === "zai" || key === "zhipu" ? <path d="M5 6h14L5 18h14" />
        : key === "grok" || key === "xai" ? <path d="M5 5l14 14M19 5L5 19M7 3h6M11 21h6" />
        : local ? <><rect x="4" y="5" width="16" height="14" rx="3" /><path d="M8 9l3 3-3 3M13 15h3" /></>
        : <><circle cx="12" cy="12" r="7" /><path d="M12 5v14M5 12h14M7 7l10 10M7 17L17 7" /></>}
    </svg>
  </span>;
}
