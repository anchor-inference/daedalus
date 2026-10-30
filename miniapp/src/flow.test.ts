import { describe, expect, it } from "vitest";
import { flowIcon, parseFlow, parseNode } from "./flow";
import { renderMarkdown } from "./md";

const PILOT = [
  "💬 Customer — writes in the chat",
  "↓",
  "🤖 ==AI assistant== — answers from the knowledge base",
  "↓ when it cannot answer",
  "✅ Solved — 80% of questions → 🎧 Operator — takes the rest",
  "note: Two weeks, one channel",
];

describe("a flow", () => {
  it("reads the header, the rows, the arrows between them and the caption", () => {
    const flow = parseFlow("💡 Concept of a small pilot project", PILOT);
    expect(flow.icon).toBe("💡");
    expect(flow.title).toBe("Concept of a small pilot project");
    expect(flow.note).toBe("Two weeks, one channel");
    expect(flow.rows.map((row) => row.nodes.map((node) => node.title))).toEqual([["Customer"], ["AI assistant"], ["Solved", "Operator"]]);
    expect(flow.steps).toEqual([{ arrow: "↓", label: "" }, { arrow: "↓", label: "when it cannot answer" }]);
    expect(flow.rows[2].links).toEqual(["→"]);
    expect(flow.rows[1].nodes[0]).toEqual({ icon: "🤖", title: "AI assistant", text: "answers from the knowledge base", mark: true });
  });

  it("puts a down arrow between rows nobody joined, and takes the two-way and upward arrows", () => {
    const flow = parseFlow("", ["A", "B", "↕", "C", "↑ back", "D"]);
    expect(flow.steps.map((step) => step.arrow)).toEqual(["↓", "↕", "↑"]);
    expect(flow.steps[2].label).toBe("back");
    expect(parseFlow("", ["A", "<->", "B"]).steps[0].arrow).toBe("↕");
  });

  it("joins nodes in a row with each kind of arrow, and with none", () => {
    const row = parseFlow("", ["A -> B <-> C | D <- E"]).rows[0];
    expect(row.nodes.map((node) => node.title)).toEqual(["A", "B", "C", "D", "E"]);
    expect(row.links).toEqual(["→", "↔", "", "←"]);
  });

  it("leaves arrows and hyphens without spaces around them inside the words", () => {
    const node = parseFlow("", ["Wi-Fi bridge — sends A->B over Wi-Fi"]).rows[0].nodes;
    expect(node).toHaveLength(1);
    expect(node[0].title).toBe("Wi-Fi bridge");
    expect(node[0].text).toBe("sends A->B over Wi-Fi");
  });

  it("takes the title from a title line, drops list bullets and a bolded title's asterisks", () => {
    const flow = parseFlow("", ["title: 🧭 Release", "- **Build** — on CI", "2. Ship"]);
    expect(flow.icon).toBe("🧭");
    expect(flow.title).toBe("Release");
    expect(flow.rows.map((row) => row.nodes[0].title)).toEqual(["Build", "Ship"]);
  });

  it("knows the common emoji as the app's line icons, a check in its colour, and keeps any other emoji", () => {
    expect(flowIcon("✅")).toEqual({ name: "checkcircle", tone: "ok" });
    expect(flowIcon("⚙️")?.name).toBe("settings");
    expect(flowIcon("🎧")?.name).toBe("headset");
    expect(flowIcon("🦄")).toBeNull();
    expect(parseNode("👩‍💻 Developer").icon).toBe("👩‍💻");
  });
});

describe("a malformed or unfinished flow", () => {
  it("never throws, whatever the lines hold", () => {
    const junk = ["", "→", "| | |", "->", "==", "```", "::: nonsense", "title:", "note:", "↓↓↓", "\u0000", "a".repeat(5000)];
    expect(() => parseFlow("", junk)).not.toThrow();
    const flow = parseFlow("", junk);
    expect(flow.rows.length).toBeGreaterThan(0);
  });

  it("drops arrows with nothing to join and keeps the later of two", () => {
    const flow = parseFlow("", ["↓", "A", "↑", "↕", "B", "↓"]);
    expect(flow.rows).toHaveLength(2);
    expect(flow.steps).toEqual([{ arrow: "↕", label: "" }]);
  });

  it("holds at most forty rows", () => {
    expect(parseFlow("", Array.from({ length: 90 }, (_, i) => `Step ${i}`)).rows).toHaveLength(40);
  });

  it("marks a node whose closing == has not streamed in yet, without showing the pair", () => {
    const node = parseNode("🤖 ==AI assis");
    expect(node).toMatchObject({ title: "AI assis", mark: true });
  });

  it("draws a block still streaming as the rows that have arrived", () => {
    const html = renderMarkdown("Here is the plan:\n\n:::flow Pilot\n💬 Customer — writes\n↓\n🤖 AI assis");
    expect(html).toContain('<figure class="flow">');
    expect(html.match(/class="flow-node/g)).toHaveLength(2);
    expect(html).toContain("AI assis");
    expect(html).not.toContain(":::");
  });

  it("draws nothing for a block that has only just opened", () => {
    expect(renderMarkdown("Plan:\n\n:::flow")).toBe("<p>Plan:</p>");
  });
});

describe("a flow on the page", () => {
  const html = renderMarkdown(":::flow 💡 Concept of a small pilot project\n" + PILOT.join("\n") + "\n:::\n\nAfter the flow.");

  it("is a panel with its header, nodes, arrows and caption, and the text after it still renders", () => {
    expect(html).toContain('<div class="flow-head"><svg class="ic ic-bulb"');
    expect(html).toContain("Concept of a small pilot project");
    expect(html).toContain('<div class="flow-node is-mark">');
    expect(html).toContain('<span class="flow-icon tone-ok"><svg class="ic ic-checkcircle"');
    expect(html).toContain('<span class="flow-link" aria-hidden="true">→</span>');
    expect(html).toContain('<span class="flow-step-label">when it cannot answer</span>');
    expect(html).toContain('<div class="flow-note">Two weeks, one channel</div>');
    expect(html).toContain("<p>After the flow.</p>");
  });

  it("escapes what the model wrote", () => {
    const evil = renderMarkdown(':::flow <img src=x onerror=alert(1)>\n<script>alert(1)</script> — "x"\n:::');
    expect(evil).not.toContain("<script>");
    expect(evil).not.toContain("<img");
    expect(evil).toContain("&lt;script&gt;");
  });

  it("is left as written inside a code fence", () => {
    expect(renderMarkdown("```\n:::flow\nA\n:::\n```")).not.toContain("flow-node");
  });
});

describe("a mermaid fence", () => {
  it("is marked for drawing once it is closed, and keeps its code for the copy button", () => {
    const html = renderMarkdown("```mermaid\nflowchart TD\n  A --> B\n```");
    expect(html).toContain('<div class="codecard mermaid-card" data-mermaid="pending">');
    expect(html).toContain("data-mermaid-toggle");
    expect(html).toContain("A --&gt; B");
  });

  it("stays a plain code block while it streams", () => {
    const html = renderMarkdown("```mermaid\nflowchart TD\n  A --> B");
    expect(html).not.toContain("data-mermaid");
    expect(html).toContain('<div class="codecard">');
  });

  it("is not drawn when empty, and other languages are untouched", () => {
    expect(renderMarkdown("```mermaid\n\n```")).not.toContain("data-mermaid");
    expect(renderMarkdown("```js\nlet a\n```")).not.toContain("data-mermaid");
  });
});

describe("the skill that teaches the syntax", () => {
  it("promises line icons only for emoji the app draws as one", async () => {
    const skill = (await import("../../skills/rich-answers/SKILL.md?raw")).default as string;
    const at = skill.indexOf("emoji as line icons");
    const listed = skill.slice(at, skill.indexOf("\n- ", at)).match(/\p{Extended_Pictographic}/gu) ?? [];
    expect(listed.length).toBeGreaterThan(20);
    expect(listed.filter((emoji) => !flowIcon(emoji))).toEqual([]);
  });
});
