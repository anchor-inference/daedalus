// The mermaid renderer itself. This module is the only one that imports mermaid, and diagrams.ts
// imports it only when an answer on screen holds a closed ```mermaid fence, so the library (several
// megabytes with its diagram types) never weighs on the app's first load.

import mermaid from "mermaid";

/** The colours and type a diagram is drawn in, read from the app's tokens; `key` names the set. */
export type MermaidTheme = { key: string; dark: boolean; font: string; vars: Record<string, string> };

let configured = "";
let counter = 0;
// One drawing at a time: mermaid keeps its configuration and its scratch element in globals, and two
// renders in flight with two themes would paint one in the other's colours.
let queue: Promise<unknown> = Promise.resolve();

/** The SVG of one diagram and the id its markup is scoped by; rejects with mermaid's error. */
export function renderMermaid(code: string, theme: MermaidTheme): Promise<{ svg: string; id: string }> {
  const run = queue.then(async () => {
    if (configured !== theme.key) {
      mermaid.initialize({
        startOnLoad: false,
        // Strict: labels are sanitised and a diagram's click handlers and links to scripts are
        // dropped, so a diagram in model text can do no more than the rest of the answer.
        securityLevel: "strict",
        // A syntax error is thrown to the caller, which shows the code; the default draws a bomb
        // picture into the page instead.
        suppressErrorRendering: true,
        theme: "base",
        darkMode: theme.dark,
        fontFamily: theme.font,
        themeVariables: { ...theme.vars, fontFamily: theme.font, fontSize: "14px" },
        flowchart: { useMaxWidth: true, padding: 12 },
        sequence: { useMaxWidth: true },
      });
      configured = theme.key;
    }
    const id = `answer-diagram-${++counter}`;
    try {
      const { svg } = await mermaid.render(id, code);
      return { svg, id };
    } finally {
      // A failed render can leave its scratch element in the body.
      document.getElementById(id)?.remove();
      document.getElementById(`d${id}`)?.remove();
    }
  });
  queue = run.catch(() => undefined);
  return run;
}
