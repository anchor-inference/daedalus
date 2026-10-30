// @vitest-environment jsdom
import { describe, expect, it, vi } from "vitest";

const renderMermaid = vi.fn(async (code: string) => {
  if (code.includes("broken")) throw new Error("Parse error on line 2:\n...\n---^");
  return { svg: `<svg id="answer-diagram-1"><g id="answer-diagram-1-node">${code.length}</g></svg>`, id: "answer-diagram-1" };
});
vi.mock("./mermaidrender", () => ({ renderMermaid }));

const { hydrateDiagrams } = await import("./diagrams");
const { renderMarkdown } = await import("./md");

function mount(text: string): HTMLElement {
  const root = document.createElement("div");
  root.innerHTML = renderMarkdown(text);
  document.body.appendChild(root);
  return root;
}

const settle = () => new Promise((resolve) => setTimeout(resolve, 0));

describe("drawing a mermaid fence", () => {
  it("puts the drawing above the code and keeps the code in the card", async () => {
    const root = mount("```mermaid\nflowchart TD\n  A --> B\n```");
    hydrateDiagrams(root);
    const card = root.querySelector(".codecard") as HTMLElement;
    expect(card.dataset.mermaid).toBe("loading");
    await vi.waitFor(() => expect(card.dataset.mermaid).toBe("done"));
    expect(card.querySelector(".codehead + .mermaid-view svg")).not.toBeNull();
    expect(card.querySelector("pre code")?.textContent).toBe("flowchart TD\n  A --> B");
  });

  it("puts a drawing back at once when the same markup is written again, as streaming does", async () => {
    const text = "```mermaid\nflowchart LR\n  X --> Y\n```";
    const first = mount(text);
    hydrateDiagrams(first);
    await vi.waitFor(() => expect(first.querySelector(".mermaid-view")).not.toBeNull());
    const calls = renderMermaid.mock.calls.length;
    const again = mount(text + "\n\nMore words arriving");
    hydrateDiagrams(again);
    expect(again.querySelector(".mermaid-view svg")).not.toBeNull();
    expect(renderMermaid.mock.calls.length).toBe(calls);
  });

  it("gives every copy of a drawing its own ids", async () => {
    const text = "```mermaid\nflowchart LR\n  P --> Q\n```";
    const one = mount(text);
    const two = mount(text);
    hydrateDiagrams(one);
    hydrateDiagrams(two);
    await vi.waitFor(() => expect(two.querySelector(".mermaid-view svg")).not.toBeNull());
    const ids = [one, two].map((root) => root.querySelector(".mermaid-view svg")?.id);
    expect(ids[0]).toMatch(/^answer-diagram-1-\d+$/);
    expect(ids[0]).not.toBe(ids[1]);
  });

  it("shows the code with a note when mermaid cannot read it", async () => {
    const root = mount("```mermaid\nflowchart TD\n  broken -->\n```");
    hydrateDiagrams(root);
    const card = root.querySelector(".codecard") as HTMLElement;
    await vi.waitFor(() => expect(card.dataset.mermaid).toBe("error"));
    expect(card.querySelector(".mermaid-view")).toBeNull();
    expect(card.querySelector(".mermaid-note")?.getAttribute("title")).toBe("Parse error on line 2:");
    expect(card.querySelector("pre")).not.toBeNull();
  });

  it("leaves a fence still streaming alone", async () => {
    const calls = renderMermaid.mock.calls.length;
    const root = mount("```mermaid\nflowchart TD\n  A -->");
    hydrateDiagrams(root);
    await settle();
    expect(renderMermaid.mock.calls.length).toBe(calls);
    expect(root.querySelector(".mermaid-view")).toBeNull();
  });

  it("switches between the drawing and the code", async () => {
    const root = mount("```mermaid\nflowchart TD\n  T --> U\n```");
    hydrateDiagrams(root);
    const card = root.querySelector(".codecard") as HTMLElement;
    await vi.waitFor(() => expect(card.dataset.mermaid).toBe("done"));
    (card.querySelector("button[data-mermaid-toggle]") as HTMLButtonElement).click();
    expect(card.dataset.view).toBe("code");
    (card.querySelector("button[data-mermaid-toggle]") as HTMLButtonElement).click();
    expect(card.dataset.view).toBe("diagram");
  });

  it("opens the drawing large and closes it with Escape", async () => {
    const root = mount("```mermaid\nflowchart TD\n  V --> W\n```");
    hydrateDiagrams(root);
    const card = root.querySelector(".codecard") as HTMLElement;
    await vi.waitFor(() => expect(card.dataset.mermaid).toBe("done"));
    (card.querySelector("button[data-mermaid-open]") as HTMLButtonElement).click();
    expect(document.querySelector(".diagram-zoom .diagram-zoom-body svg")).not.toBeNull();
    document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape" }));
    expect(document.querySelector(".diagram-zoom")).toBeNull();
  });
});
