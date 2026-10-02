// A half-translated screen is worse than an untranslated one: the reader cannot tell whether the
// English sentence in the middle of a Russian page is a gap or a term of art. So the dictionary is
// checked as a table — every key present in both languages, neither column left as a copy of the
// other where a translation was meant, and every key the code asks for actually in it.

/// <reference types="vite/client" />
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { DICT, LANGS, plural, setLang, t } from "./i18n";

// Every source file under src/, as text. Read through the bundler rather than from disk: the app
// has no Node types, and importing the modules themselves would run code that wants a browser.
const SOURCES = import.meta.glob("./**/*.{ts,tsx}", { query: "?raw", import: "default", eager: true }) as Record<string, string>;

/** The rail's destinations, read out of the router rather than imported. */
function screens(): string[] {
  const found = SOURCES["./router.ts"].match(/export const SCREENS: Screen\[\] = \[([^\]]+)\]/);
  if (!found) throw new Error("the router no longer lists its screens where this test looks");
  return [...found[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

/** A list of string literals a module exports, read out of its source for the same reason. */
function listed(file: string, name: string): string[] {
  const found = SOURCES[file].match(new RegExp(`export const ${name}[^=]*=\\s*\\[([^\\]]+)\\]`));
  if (!found) throw new Error(`${name} is no longer declared in ${file} where this test looks`);
  return [...found[1].matchAll(/"([^"]+)"/g)].map((m) => m[1]);
}

// Words that are the same in both languages: names, brands and the ids of tools and config fields.
// Everything else being identical means a row was copied and never translated.
const SAME_IN_BOTH = [
  // The agent's name on its browser cursor is the product's name in both languages.
  "browser.agent",
  // Three of the components are proper names of programs and are spelled the same in Russian.
  "comp.name.git",
  "comp.name.node",
  "comp.name.ripgrep",
  // The name of the composer's switch is the principle's own acronym, which the operator reads as is in either language.
  "composer.yagni",
  "fmt.cron.utc",
  // A CLI's own words for the account it is signed in with, and a dash where it is not installed.
  "harness.signin.absent",
  "harness.signin.as",
  "lang.name.en",
  "lang.name.ru",
  "login.title",
  // Channel names that are product names in both languages, and rows made only of holes.
  "nset.cell.aria",
  "nset.channel.push",
  "nset.channel.telegram",
  "nset.out.device",
  "nset.test.line",
  "sched.cron",
  "sched.when.cron",
  "session.mcp.toggled",
  // The fallback pill's second name: an arrow and the configured model, nothing to translate.
  "session.model.fallback",
  "session.sched.cron",
  // Today's spend is a row made only of holes and arrows.
  "session.usage.today.line",
  // The chat's own product name heads its card.
  "settings.chat.telegram",
  "settings.exec.title",
  "settings.search.title",
  "settings.web.title",
  // Lines changed and the files they are in.
  "staff.changes.summary",
  // The key before a member's CLI and its version: the acronym is read as is in either language.
  "staff.details.cli",
  "theme.claude",
  // A tool named by its own id and a count: the id is the same in both languages.
  "turn.family.other",
  // The update dialog's title is the product's name and the release's number.
  "update.title",
  "usage.col.usd",
];

/** A row whose forms are separated by `|` is a plural: English has two, Russian three. */
const isPlural = (key: string) => DICT[key].en.includes("|");

describe("the dictionary", () => {
  it("has every key in every language", () => {
    const missing: string[] = [];
    for (const [key, entry] of Object.entries(DICT)) {
      for (const lang of LANGS) {
        if (!entry[lang] || !entry[lang].trim()) missing.push(`${key}.${lang}`);
      }
    }
    expect(missing).toEqual([]);
  });

  it("gives a plural row two English forms and three Russian ones", () => {
    const wrong: string[] = [];
    for (const key of Object.keys(DICT)) {
      if (!isPlural(key)) continue;
      if (DICT[key].en.split("|").length !== 2) wrong.push(`${key}.en`);
      if (DICT[key].ru.split("|").length !== 3) wrong.push(`${key}.ru`);
    }
    expect(wrong).toEqual([]);
  });

  it("translates rather than repeating, outside the handful of words that are the same in both", () => {
    const same = Object.entries(DICT)
      .filter(([, entry]) => entry.en === entry.ru)
      .map(([key]) => key);
    expect(same.sort()).toEqual(SAME_IN_BOTH);
  });

  it("keeps the same holes in both languages", () => {
    const holes = (text: string) => [...new Set([...text.matchAll(/\{(\w+)\}/g)].map((m) => m[1]))].sort();
    for (const [key, entry] of Object.entries(DICT)) {
      expect(holes(entry.ru), key).toEqual(holes(entry.en));
    }
  });

  // The product's tone: calm. It survives a translation only if it is checked in the translation.
  it("keeps the Russian free of exclamation marks", () => {
    const shouting = Object.entries(DICT)
      .filter(([, entry]) => entry.ru.includes("!"))
      .map(([key]) => key);
    expect(shouting).toEqual([]);
  });
});

// Every language code either speech catalog can name. The recognition catalog is the longer of the
// two and the synthesis one is a subset of it, so this list is the recognition side's.
const SPOKEN = [
  "ar", "be", "bg", "cs", "da", "de", "el", "en", "es", "et", "fi", "fr", "he", "hi", "hr", "hu",
  "id", "it", "ja", "ko", "lt", "lv", "mt", "nb", "nl", "pl", "pt", "ro", "ru", "sk", "sl", "sv",
  "th", "tr", "uk", "vi", "yue", "zh",
];

// The component ids, as daedalus/host/components.py orders them.
const COMPONENTS = ["speech", "stt-models", "tts-voices", "browser", "node", "git", "ripgrep", "opus", "bwrap"];

describe("the keys the code asks for", () => {
  it("are all in the table", () => {
    const missing = new Set<string>();
    for (const [path, text] of Object.entries(SOURCES)) {
      if (path.endsWith(".test.ts")) continue;
      // `t`, the aliases a screen imports it under where the name is taken, and `plural`.
      for (const [, key] of text.matchAll(/\b(?:t|t2|plural)\("([^"]+)"/g)) {
        if (!(key in DICT)) missing.add(`${key} (${path})`);
      }
    }
    expect([...missing]).toEqual([]);
  });

  it("uses plural() for every row that has plural forms, and only for those", () => {
    const asPlural = new Set<string>();
    const asPlain = new Set<string>();
    for (const [path, text] of Object.entries(SOURCES)) {
      if (path.endsWith(".test.ts")) continue;
      for (const [, key] of text.matchAll(/\bplural\("([^"]+)"/g)) asPlural.add(key);
      for (const [, key] of text.matchAll(/\b(?:t|t2)\("([^"]+)"/g)) asPlain.add(key);
    }
    expect([...asPlural].filter((k) => k in DICT && !isPlural(k))).toEqual([]);
    expect([...asPlain].filter((k) => k in DICT && isPlural(k))).toEqual([]);
  });

  // The keys built from a value rather than written out. A screen added to the rail without a name,
  // a state the app has no word for, or a tool nobody named shows its own key on the page.
  it("includes every key built from a name", () => {
    const families: [string, string[]][] = [
      ["nav.", screens()],
      ["nav.group.", ["work", "autonomy", "knowledge", "observe"]],
      // The panel names its tabs from the union that defines them, and the voice screen names the
      // phase it is in from the reducer's own. A fifth tab or a seventh phase otherwise ships as a
      // bare `[panel.tab.x]` with every test here green.
      ["panel.tab.", listed("./panel.ts", "PANEL_TABS")],
      ["voice.phase.", listed("./voice.ts", "VOICE_PHASES")],
      ["add.effort.", ["low", "medium", "high", "xhigh"]],
      ["lang.name.", [...LANGS]],
      ["status.", ["idle", "running", "waiting", "compacting", "failed", "done", "paused", "stopped", "pending", "merged", "approved", "rejected", "closed", "dead"]],
      ["fmt.dow.", ["0", "1", "2", "3", "4", "5", "6"]],
      ["fmt.dur.", ["s", "m", "h", "d"]],
      ["board.col.", ["todo", "doing", "review", "blocked", "done", "dropped"]],
      ["sched.kind.", ["agent", "message", "lazy"]],
      ["sched.group.", ["upcoming", "paused", "done"]],
      ["sched.when.", ["once", "daily", "weekdays", "weekly", "hours", "cron"]],
      ["memory.kind.", ["fact", "decision", "preference", "reflection", "skill", "note"]],
      ["usage.purpose.", ["stream", "structured", "text"]],
      ["svc.access.", ["local", "key", "public"]],
      ["svc.copied.", ["address", "link", "key"]],
      ["settings.chat.", ["private", "topics"]],
      ["settings.selfchange.", ["manual", "auto"]],
      ["settings.sec.", ["appearance", "models", "rules", "limits", "environments", "tools", "voice", "components", "chat", "notifications", "security", "heartbeat", "about"]],
      ["theme.", listed("./appearance.ts", "THEME_IDS")],
      ["tool.group.", ["Exec", "Read", "Write", "Edit", "search", "WebFetch", "SendFile", "other"]],
      ["tool.board.", ["get", "list"]],
      // Both speech pickers build a language name from a catalog's own code, and the recognition
      // one reaches far past the ten languages the synthesis catalog speaks. A code with no row
      // here is a bare `[lang.of.xx]` in a filter someone is choosing from.
      ["lang.of.", SPOKEN],
      ["stt.progress.", ["downloading", "verifying", "unpacking", "queued", "failed"]],
      // Every component the registry can name, in both halves of its card, plus the four states and
      // every capability key a card lists. A component added to daedalus/host/components.py with no
      // row here is a bare `[comp.name.x]` on the page that exists to explain it.
      ["comp.name.", COMPONENTS],
      ["comp.what.", COMPONENTS],
      ["comp.state.", ["installed", "missing", "installing", "unavailable"]],
      ["comp.enables.", ["stt", "tts", "voicenotes", "skills.browser", "screenshots", "skills.node", "npx", "selfdev", "projects", "search", "sandbox"]],
      ["comp.mode.", ["native", "docker"]],
      // A staff member's status, isolation and colour come from the host by name; each has a word.
      ["team.status.", listed("./team/team.ts", "STAFF_STATUSES")],
      ["team.isolation.", listed("./team/team.ts", "ISOLATIONS")],
      ["team.colour.", listed("./team/team.ts", "STAFF_COLOURS")],
      ["team.env.", ["container", "host"]],
      ["team.unavailable.", ["notinstalled", "loggedout", "error"]],
      // A project board names its columns, the brief's parts and the kinds of request from lists
      // it builds; each needs a word, and a column also its "nothing here" line.
      ["pboard.col.", listed("./board/board.ts", "COLUMNS")],
      ["pboard.none.", listed("./board/board.ts", "COLUMNS")],
      ["pboard.brief.", listed("./board/board.ts", "BRIEF_FIELDS")],
      ["pboard.need.kind.", ["question", "permission", "folder"]],
      // Why Merge is disabled comes from the host as a code; each code the app knows has its words.
      ["pboard.review.block.", listed("./board/board.ts", "BLOCKER_CODES")],
      // A project's focus mode: the panel's project tabs, the orchestrator's steps, why a launch
      // waits, the pages, the colours of a member's dot, the brief's sections, the journal's kinds
      // and authors, and a message's receipt all name a word from a list.
      ["panel.tab.", listed("./panel.ts", "PROJECT_TABS")],
      ["focus.step.", listed("./project/focus.ts", "STEP_KEYS")],
      ["focus.wait.", listed("./project/focus.ts", "WAIT_REASONS")],
      ["focus.page.", listed("./project/focus.ts", "FOCUS_PAGES")],
      ["focus.autonomy.", listed("./project/focus.ts", "AUTONOMIES")],
      ["focus.tone.", ["working", "review", "waiting", "free", "off", "silent", "error"]],
      ["focus.brief.section.", listed("./project/pages.tsx", "BRIEF_SECTIONS")],
      ["focus.kind.", listed("./project/pages.tsx", "JOURNAL_KINDS")],
      ["focus.author.", ["operator", "orchestrator", "staff", "system"]],
      ["focus.ask.who.", ["operator", "orchestrator", "system"]],
      ["focus.msg.state.", ["queued", "written", "submitted", "acknowledged", "failed"]],
      ["focus.msg.from.", ["orchestrator", "operator"]],
      // A notification's category and the way its request ended come from the host by name.
      ["notice.cat.", listed("./notifications.tsx", "NOTICE_CATEGORIES")],
      ["notice.resolution.", listed("./notifications.tsx", "NOTICE_RESOLUTIONS")],
      // The notification settings name every category, channel, cell and mute length from lists.
      ["nset.cat.", listed("./notifications.tsx", "NOTICE_CATEGORIES")],
      ["nset.channel.", listed("./notifyprefs.ts", "CHANNELS")],
      ["nset.cell.", listed("./notifyprefs.ts", "CELLS")],
      ["nset.legend.", listed("./notifyprefs.ts", "CELLS")],
      ["nset.cell.", listed("./notifyprefs.ts", "CELLS").map((c) => `${c}.long`)],
      ["nset.mute.", listed("./notifyprefs.ts", "MUTE_ENDS")],
      ["load.basis.", ["running", "measured", "default"]],
      // A phone names each key of its terminal's row for a screen reader, the project's tabs, and the
      // environment pill's explanation.
      ["term.phone.key.", [...SOURCES["./terminal/phonekeys.ts"].matchAll(/\{ id: "([^"]+)", cap:/g)].map((m) => m[1])],
      ["phone.tab.", listed("./project/focus.ts", "PHONE_TABS")],
      ["term.env.title.", ["container", "host"]],
      // A command-line member's view names the state of its team tools, its column's tabs, who
      // answered a request first and its two views from lists the host and the model share.
      ["staff.health.tools.", ["connected", "missing", "waiting", "builtin", "none"]],
      ["staff.tab.", ["session", "changes", "notes"]],
      ["staff.mode.", ["feed", "terminal"]],
      ["perm.by.", ["operator", "orchestrator", "terminal"]],
      ["harness.channel.", ["hooks", "app_server", "sse", "extension", "files", "acp"]],
      // The Harnesses screen names a row's button, its version marker and an operation by code.
      ["harness.state.", ["current", "needsnode", "absent", "install", "signin", "update"]],
      ["harness.mark.", ["unsupported", "unverified", "verified"]],
      ["harness.op.", ["check", "update", "install", "signin"]],
      ["questions.section.", ["requests", "questions"]],
      ["questions.perm.", ["allow", "always", "deny", "because"]],
      ["questions.field.", ["answer", "note", "reason"]],
      ["panel.tab.", ["questions"]],
    ];
    const missing = families.flatMap(([prefix, names]) => names.map((n) => prefix + n)).filter((key) => !(key in DICT));
    // The section hints sit beside the section names, and a hint nobody wrote is a blank line.
    const hints = ["appearance", "models", "rules", "limits", "environments", "tools", "voice", "components", "chat", "notifications", "security", "heartbeat", "about"].map((s) => `settings.sec.${s}.hint`).filter((k) => !(k in DICT));
    // Every tool the timeline names has a verb while it runs and one after it.
    const verbs = ["Exec", "Read", "Write", "Edit", "Find", "WebSearch", "WebFetch", "SendFile", "ImageView", "Skill", "Verify", "SubAgent", "SpawnAgent", "AskPeer", "HistorySearch", "ServiceStart", "ServiceStop"]
      .flatMap((name) => [`tool.${name}.on`, `tool.${name}.off`])
      .filter((k) => !(k in DICT));
    const isolation = listed("./team/team.ts", "ISOLATIONS").flatMap((i) => [`team.isolation.${i}.short`, `team.isolation.${i}.hint`]).filter((k) => !(k in DICT));
    const placeholders = listed("./board/board.ts", "BRIEF_FIELDS").map((f) => `pboard.brief.${f}.placeholder`).filter((k) => !(k in DICT));
    const autonomy = listed("./project/focus.ts", "AUTONOMIES").map((a) => `focus.autonomy.${a}.hint`).filter((k) => !(k in DICT));
    expect([...missing, ...hints, ...verbs, ...isolation, ...placeholders, ...autonomy]).toEqual([]);
  });
});

// Which language a visit is in is decided once, as the module loads, and the order matters: the
// launcher names one in the address, and that has to beat both the reader's last choice here and
// the browser's own preference. The module reads the world at import time, so the world is built
// here first and the module imported after it.
describe("which language a visit is in", () => {
  const kept = new Map<string, string>();

  beforeEach(() => {
    vi.resetModules();
    kept.clear();
  });
  afterEach(() => vi.unstubAllGlobals());

  async function visit(search: string, browser: string, chosen?: string) {
    if (chosen) kept.set("daedalus.lang", chosen);
    const replaced: string[] = [];
    vi.stubGlobal("window", {
      location: { href: "http://127.0.0.1:8765/app/" + search, pathname: "/app/", search, hash: "" },
      history: { state: null, replaceState: (_s: unknown, _t: string, next: string) => replaced.push(next) },
    });
    vi.stubGlobal("localStorage", {
      getItem: (key: string) => kept.get(key) ?? null,
      setItem: (key: string, value: string) => void kept.set(key, value),
    });
    vi.stubGlobal("navigator", { language: browser, languages: [browser] });
    const module = await import("./i18n");
    return { lang: module.lang(), replaced };
  }

  it("takes the language the address names, keeps it, and takes it out of the address", async () => {
    const visited = await visit("?lang=ru", "en-GB");
    expect(visited.lang).toBe("ru");
    expect(kept.get("daedalus.lang")).toBe("ru");
    expect(visited.replaced).toEqual(["/app/"]);
  });

  it("falls back to what the reader chose here, and then to the browser", async () => {
    expect((await visit("", "en-GB", "ru")).lang).toBe("ru");
    vi.resetModules();
    kept.clear();
    expect((await visit("", "ru-RU")).lang).toBe("ru");
    vi.resetModules();
    kept.clear();
    expect((await visit("", "en-GB")).lang).toBe("en");
  });
});

describe("t", () => {
  it("fills the holes, and shows a key it does not know rather than hiding it", () => {
    setLang("en");
    expect(t("add.pill.context", { n: "128k" })).toBe("128k context");
    setLang("ru");
    expect(t("add.pill.context", { n: "128k" })).toBe("контекст 128k");
    expect(t("nothing.like.this")).toBe("[nothing.like.this]");
    setLang("en");
  });
});

describe("plural", () => {
  it("counts in English by whether it is one", () => {
    setLang("en");
    expect(plural("agents.count", 1)).toBe("1 agent");
    expect(plural("agents.count", 2)).toBe("2 agents");
    expect(plural("agents.count", 0)).toBe("0 agents");
  });

  it("counts in Russian by the last digit, with the teens apart", () => {
    setLang("ru");
    expect(plural("agents.count", 1)).toBe("1 агент");
    expect(plural("agents.count", 2)).toBe("2 агента");
    expect(plural("agents.count", 5)).toBe("5 агентов");
    expect(plural("agents.count", 11)).toBe("11 агентов");
    expect(plural("agents.count", 21)).toBe("21 агент");
    expect(plural("agents.count", 22)).toBe("22 агента");
    expect(plural("agents.count", 112)).toBe("112 агентов");
    setLang("en");
  });

  it("shows a key it does not know", () => {
    expect(plural("nothing.like.this", 3)).toBe("[nothing.like.this]");
  });
});
