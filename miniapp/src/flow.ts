// A flow in an answer: a process, a pipeline or a small architecture drawn as rows of centred node
// blocks joined by arrows, in one bordered panel. The model writes it in the same family as a card,
// in a form that still reads as a diagram where nothing draws it (Telegram, an export, a copy):
//
//   :::flow 💡 Concept of a small pilot project      the panel's header: an emoji, then the title
//   💬 Customer — writes in the chat                  a row of one node: icon, title — subtitle
//   ↓                                                 the arrow to the next row (the default)
//   🤖 ==AI assistant== — answers from the base       ==…== is the highlighted node
//   ↓ when it cannot answer                           an arrow may carry a short label
//   ✅ Solved — 80% → 🎧 Operator — the rest          nodes side by side: → ← ↔ or | (no arrow)
//   note: Two weeks, one channel                      the muted caption under the panel
//   :::
//
// An emoji leads a node because it is what a model writes before a step by habit and what a chat
// shows well: the app swaps the common ones for its own line icons, keeps any other as it is, and
// Telegram keeps them all. A named icon (`icon: bot`) was the alternative and was rejected: a word
// the model has to remember from a list, and noise in every client that does not draw the panel.

import type { IconName } from "./icons";

export type FlowNode = { icon: string; title: string; text: string; mark: boolean };
/** What joins two nodes in a row: an arrow, or nothing for nodes that merely stand side by side. */
export type FlowLink = "→" | "←" | "↔" | "";
export type FlowRow = { nodes: FlowNode[]; links: FlowLink[] };
/** What joins a row to the next one: an arrow down, up, both ways, and the words on it. */
export type FlowStep = { arrow: "↓" | "↑" | "↕"; label: string };
export type Flow = { icon: string; title: string; note: string; rows: FlowRow[]; steps: FlowStep[] };

export const FLOW_OPEN_RE = /^\s*:::\s*flow\b\s*(.*)$/i;
const KEY_RE = /^\s*(title|note)\s*:\s?(.*)$/i;
// A line that is only an arrow (and maybe its label) joins two rows. The ASCII letters count only
// alone on their line: "v2 ships" is a node, not an arrow with a label.
const STEP_RE = /^\s*(↑↓|↓↑|⇅|⇵|↕|↓|↑|⬇️?|⬆️?|↧|↥)(?:\s+(.*?))?\s*$/u;
const STEP_ASCII_RE = /^\s*(v|\^|\^v|v\^|<->)\s*$/i;
// Inside a row the arrows need a space on each side, so "A->B" in a subtitle or a path stays text.
const LINK_RE = /\s+(<->|↔|<-->|-->|->|→|<--|<-|←|\|)\s+/u;
const BULLET_RE = /^\s*(?:[-*+]|\d+[.)])\s+/;
// The emoji a node starts with, with its variation selector, skin tone and joined parts.
const EMOJI_RE = /^((?:\p{Extended_Pictographic}|\p{Regional_Indicator}{2})(?:️|⃣|\p{Emoji_Modifier}|‍\p{Extended_Pictographic}️?)*)\s*/u;
// The first spaced dash splits the title from the subtitle; a hyphen inside a word ("Wi-Fi") does not.
const SUBTITLE_RE = /\s+[—–-]\s+/;
const MAX_ROWS = 40;

function step(raw: string): FlowStep["arrow"] {
  if (/↑↓|↓↑|⇅|⇵|↕|<->|\^v|v\^/i.test(raw)) return "↕";
  if (/↑|⬆|↥|\^/.test(raw)) return "↑";
  return "↓";
}

function link(raw: string): FlowLink {
  if (raw === "|") return "";
  if (raw === "<->" || raw === "<-->" || raw === "↔") return "↔";
  if (raw === "<-" || raw === "<--" || raw === "←") return "←";
  return "→";
}

/** One node's text: `💬 Title — subtitle`, `==` around any part of it marking the highlighted node. */
export function parseNode(raw: string): FlowNode {
  let text = raw.trim();
  // Marked while it streams too: the closing `==` has not arrived yet, and the reader should not see
  // the opening pair as text for that moment.
  const mark = /==(?=\S)/.test(text);
  if (mark) text = text.replace(/==(?=\S)|(?<=\S)==/g, "");
  const emoji = text.match(EMOJI_RE);
  const icon = emoji ? emoji[1] : "";
  if (emoji) text = text.slice(emoji[0].length);
  const split = text.search(SUBTITLE_RE);
  let title = split >= 0 ? text.slice(0, split) : text;
  const subtitle = split >= 0 ? text.slice(split).replace(SUBTITLE_RE, "") : "";
  // The title is drawn bold already; a model that bolds it as well should not get asterisks.
  title = title.trim().replace(/^\*\*(.+)\*\*$/, "$1");
  return { icon, title, text: subtitle.trim(), mark };
}

function parseRow(line: string): FlowRow | null {
  const parts = line.replace(BULLET_RE, "").split(LINK_RE);
  const nodes: FlowNode[] = [];
  const links: FlowLink[] = [];
  for (let index = 0; index < parts.length; index += 2) {
    const node = parseNode(parts[index]);
    if (!node.title && !node.text && !node.icon) continue;
    if (nodes.length) links.push(link(parts[index - 1] ?? "|"));
    nodes.push(node);
  }
  return nodes.length ? { nodes, links } : null;
}

/**
 * The lines between `:::flow` and `:::` as a flow; `rest` is what followed the opener on its line.
 *
 * It never throws and takes any text: a line that is neither a key nor an arrow is a row, so a
 * malformed block becomes a column of plain nodes rather than a broken message, and a block still
 * streaming is the rows that have arrived. An arrow line before the first row or after the last is
 * dropped; two in a row keep the later one.
 */
export function parseFlow(rest: string, lines: readonly string[]): Flow {
  const flow: Flow = { icon: "", title: "", note: "", rows: [], steps: [] };
  const head = (raw: string) => {
    const emoji = raw.trim().match(EMOJI_RE);
    flow.icon = emoji ? emoji[1] : flow.icon;
    flow.title = (emoji ? raw.trim().slice(emoji[0].length) : raw).trim();
  };
  if (rest.trim()) head(rest);
  let pending: FlowStep | null = null;
  for (const line of lines) {
    if (!line.trim() || /^\s*```/.test(line)) continue;
    const key = line.match(KEY_RE);
    if (key) {
      if (key[1].toLowerCase() === "title") head(key[2]);
      else flow.note = key[2].trim();
      continue;
    }
    const arrow = line.match(STEP_RE) ?? line.match(STEP_ASCII_RE);
    if (arrow) {
      pending = { arrow: step(arrow[1]), label: (arrow[2] ?? "").trim() };
      continue;
    }
    if (flow.rows.length >= MAX_ROWS) continue;
    const row = parseRow(line);
    if (!row) continue;
    if (flow.rows.length) flow.steps.push(pending ?? { arrow: "↓", label: "" });
    flow.rows.push(row);
    pending = null;
  }
  return flow;
}

// The emoji the app draws in its own line icons, so a flow looks like one drawing and not a row of
// stickers. A check and a cross keep their meaning in colour as well; anything not listed is shown
// as the emoji itself.
const ICONS: Record<string, { name: IconName; tone?: "ok" | "warn" | "bad" }> = {};
const NAMED: [string, IconName, ("ok" | "warn" | "bad")?][] = [
  ["💬🗨🗯", "chat"], ["🤖", "bots"], ["✅☑✔", "checkcircle", "ok"], ["❌✖⛔🚫", "close", "bad"],
  ["⚠❗", "alert", "warn"], ["🎧", "headset"], ["👤👥🧑👩👨🙋", "user"], ["📄📃📑", "file"], ["📝✏🖊", "pen"],
  ["📁📂🗂", "folder"], ["🌐🌍🌎🌏", "globe"], ["🔍🔎", "search"], ["🗄💾🗃", "database"], ["🖥💻", "monitor"],
  ["⚙", "settings"], ["🔧🛠🔨", "wrench"], ["💡🧠", "bulb"], ["🔒🔐", "lock"], ["🔑🗝", "key"],
  ["⏱⏰🕒⌛⏳", "clock"], ["📅🗓📆", "calendar"], ["📊📈📉", "chart"], ["🛒", "cart"], ["🏠🏢", "home"],
  ["📞☎📱", "phone"], ["📧✉📨📩", "mail"], ["📥", "inbox"], ["☁", "cloud"], ["⚡🚀", "bolt"], ["🔗", "link"],
  ["🛡", "shield"], ["🔔", "bell"], ["📦", "archive"], ["🧩🔌", "plug"], ["🎤🎙", "mic"], ["🖼📷", "image"],
  ["▶", "play"], ["🔄🔁", "loop"], ["🧭", "compass"], ["📚📖📘", "book"], ["❓", "question"], ["🏁🚩", "flag"],
  ["🧾", "journal"], ["👀👁", "eye"], ["💳💰", "cart"], ["🧪", "wand"],
];
for (const [glyphs, name, tone] of NAMED) for (const glyph of glyphs.match(/\p{Extended_Pictographic}/gu) ?? []) ICONS[glyph] = { name, tone };

/** The line icon an emoji stands for, or null when it is shown as itself. */
export function flowIcon(emoji: string): { name: IconName; tone?: "ok" | "warn" | "bad" } | null {
  const base = emoji.match(/\p{Extended_Pictographic}/u)?.[0];
  return base ? ICONS[base] ?? null : null;
}
