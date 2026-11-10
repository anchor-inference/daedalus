// Starting boards for a new canvas, written as JSON Canvas so they open the same here and in Obsidian.
// Edges name their sides: a template is a picture of how the format connects things, and a side left to
// the default would change with the first card the operator drags.

import { t } from "../i18n";
import type { CanvasDoc, CanvasEdge, CanvasNode, Side } from "./model";

export type TemplateId = "blank" | "flowchart" | "mindmap" | "lanes";
export const TEMPLATES: TemplateId[] = ["blank", "flowchart", "mindmap", "lanes"];

function card(id: string, x: number, y: number, text: string, color?: string, width = 200, height = 70): CanvasNode {
  return { id, type: "text", text, x, y, width, height, ...(color ? { color } : {}) };
}

function edge(id: string, fromNode: string, fromSide: Side, toNode: string, toSide: Side, label?: string, arrow = true): CanvasEdge {
  return { id, fromNode, fromSide, toNode, toSide, ...(arrow ? {} : { toEnd: "none" as const }), ...(label ? { label } : {}) };
}

/** The document of a template in the current language; an empty board for blank. */
export function templateDoc(id: TemplateId): CanvasDoc {
  if (id === "flowchart") {
    return {
      nodes: [
        card("start", 0, 0, t("canvas.tpl.flow.start"), "4"),
        card("step", 0, 140, t("canvas.tpl.flow.step"), "5"),
        card("check", 0, 280, t("canvas.tpl.flow.check"), "3"),
        card("done", 0, 440, t("canvas.tpl.flow.done"), "4"),
        card("fix", 320, 280, t("canvas.tpl.flow.fix"), "1"),
      ],
      edges: [
        edge("e1", "start", "bottom", "step", "top"),
        edge("e2", "step", "bottom", "check", "top"),
        edge("e3", "check", "bottom", "done", "top", t("canvas.tpl.flow.yes")),
        edge("e4", "check", "right", "fix", "left", t("canvas.tpl.flow.no")),
        edge("e5", "fix", "top", "step", "right"),
      ],
    };
  }
  if (id === "mindmap") {
    const places: [number, number, string, Side, Side][] = [[0, 0, "5", "left", "right"], [0, 300, "4", "left", "right"], [640, 0, "3", "right", "left"], [640, 300, "1", "right", "left"]];
    return {
      nodes: [card("idea", 300, 140, t("canvas.tpl.mind.idea"), "6", 240, 90), ...places.map(([x, y, color], index) => card(`b${index + 1}`, x, y, t("canvas.tpl.mind.branch", { n: index + 1 }), color))],
      edges: places.map(([, , , from, to], index) => edge(`e${index + 1}`, "idea", from, `b${index + 1}`, to, undefined, false)),
    };
  }
  if (id === "lanes") {
    return {
      nodes: [
        { id: "lane-you", type: "group", label: t("canvas.tpl.lane.you"), x: 0, y: 0, width: 1000, height: 180 },
        { id: "lane-agent", type: "group", label: t("canvas.tpl.lane.agent"), x: 0, y: 220, width: 1000, height: 180 },
        card("ask", 40, 60, t("canvas.tpl.lane.ask"), "5", 180, 70),
        card("plan", 300, 280, t("canvas.tpl.lane.plan"), "6", 180, 70),
        card("review", 560, 60, t("canvas.tpl.lane.review"), "3", 180, 70),
        card("ship", 780, 280, t("canvas.tpl.lane.ship"), "4", 180, 70),
      ],
      edges: [edge("e1", "ask", "bottom", "plan", "left"), edge("e2", "plan", "right", "review", "bottom"), edge("e3", "review", "right", "ship", "top")],
    };
  }
  return { nodes: [], edges: [] };
}
