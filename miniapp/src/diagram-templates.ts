// Starter scenes for a new diagram, as Excalidraw element skeletons. The editor turns a skeleton into
// real elements with Excalidraw's own converter, which measures the labels with the canvas and binds
// the arrows to their shapes; written out by hand here, every label would be a guess at a font's
// width and every arrow would stay put when its box is dragged.

import { t } from "./i18n";

export type Skeleton = {
  type: "rectangle" | "ellipse" | "diamond" | "arrow" | "text";
  id?: string;
  x: number;
  y: number;
  width?: number;
  height?: number;
  strokeColor?: string;
  backgroundColor?: string;
  strokeStyle?: "solid" | "dashed";
  text?: string;
  fontSize?: number;
  roundness?: { type: number } | null;
  label?: { text: string; strokeColor?: string };
  start?: { id: string };
  end?: { id: string };
  points?: [number, number][];
  endArrowhead?: null | "arrow";
};

export type TemplateId = "blank" | "flowchart" | "mindmap" | "swimlane";
export const TEMPLATES: TemplateId[] = ["blank", "flowchart", "mindmap", "swimlane"];

/** Excalidraw's own palette: a stroke and the light fill that goes with it. */
const COLOURS = {
  blue: ["#1971c2", "#a5d8ff"],
  green: ["#2f9e44", "#b2f2bb"],
  yellow: ["#f08c00", "#ffec99"],
  red: ["#e03131", "#ffc9c9"],
  violet: ["#6741d9", "#d0bfff"],
  grey: ["#495057", "#e9ecef"],
} as const;

/** Label ink: dark on the light fills, and Excalidraw's dark theme turns it light with the fills. */
const INK = "#1e1e1e";
const GAP = 8;

function box(type: "rectangle" | "ellipse" | "diamond", id: string, x: number, y: number, text: string, colour: keyof typeof COLOURS, width = 180, height = 80): Skeleton {
  const [strokeColor, backgroundColor] = COLOURS[colour];
  return { type, id, x, y, width, height, strokeColor, backgroundColor, roundness: type === "ellipse" ? null : { type: 3 }, label: { text, strokeColor: INK } };
}

/** How far from a shape's centre its outline lies along the unit direction (ux, uy). */
function reach(shape: Skeleton, ux: number, uy: number): number {
  const a = (shape.width ?? 0) / 2;
  const b = (shape.height ?? 0) / 2;
  if (shape.type === "ellipse") return 1 / Math.sqrt((ux / a) ** 2 + (uy / b) ** 2);
  if (shape.type === "diamond") return 1 / (Math.abs(ux) / a + Math.abs(uy) / b);
  return Math.min(ux ? a / Math.abs(ux) : Infinity, uy ? b / Math.abs(uy) : Infinity);
}

/** An arrow from one shape's outline to the other's. The converter binds the ends but leaves the
 *  geometry where it is given, so the line is drawn here from centre to centre and cut at both outlines. */
function link(from: Skeleton, to: Skeleton, text?: string, endArrowhead: null | "arrow" = "arrow"): Skeleton {
  const centre = (shape: Skeleton) => [shape.x + (shape.width ?? 0) / 2, shape.y + (shape.height ?? 0) / 2];
  const [ax, ay] = centre(from);
  const [bx, by] = centre(to);
  const length = Math.hypot(bx - ax, by - ay) || 1;
  const ux = (bx - ax) / length;
  const uy = (by - ay) / length;
  const start = reach(from, ux, uy) + GAP;
  const end = length - reach(to, ux, uy) - GAP;
  const x = Math.round(ax + ux * start);
  const y = Math.round(ay + uy * start);
  const dx = Math.round(ux * (end - start));
  const dy = Math.round(uy * (end - start));
  return {
    type: "arrow", x, y, width: Math.abs(dx), height: Math.abs(dy), points: [[0, 0], [dx, dy]],
    start: { id: from.id! }, end: { id: to.id! }, endArrowhead, ...(text ? { label: { text, strokeColor: INK } } : {}),
  };
}

/** The skeleton of a template in the current language; an empty list for the blank canvas. */
export function templateSkeleton(id: TemplateId): Skeleton[] {
  if (id === "flowchart") {
    const start = box("ellipse", "start", 60, 0, t("diagrams.tpl.flow.start"), "green", 180, 70);
    const step = box("rectangle", "step", 60, 140, t("diagrams.tpl.flow.step"), "blue");
    const check = box("diamond", "check", 40, 290, t("diagrams.tpl.flow.check"), "yellow", 220, 130);
    const done = box("ellipse", "done", 60, 500, t("diagrams.tpl.flow.done"), "green", 180, 70);
    const fix = box("rectangle", "fix", 380, 315, t("diagrams.tpl.flow.fix"), "red");
    return [start, step, check, done, fix, link(start, step), link(step, check), link(check, done, t("diagrams.tpl.flow.yes")), link(check, fix, t("diagrams.tpl.flow.no")), link(fix, step)];
  }
  if (id === "mindmap") {
    const centre = box("ellipse", "idea", 300, 200, t("diagrams.tpl.mind.idea"), "violet", 220, 100);
    const places: [number, number, keyof typeof COLOURS][] = [[0, 40, "blue"], [0, 360, "green"], [640, 40, "yellow"], [640, 360, "red"]];
    const branches = places.map(([x, y, colour], index) => box("rectangle", `b${index + 1}`, x, y, t("diagrams.tpl.mind.branch", { n: index + 1 }), colour));
    return [centre, ...branches, ...branches.map((branch) => link(centre, branch, undefined, null))];
  }
  if (id === "swimlane") {
    const lane = (key: string, y: number, text: string): Skeleton[] => [
      { type: "rectangle", id: key, x: 0, y, width: 1000, height: 160, strokeColor: COLOURS.grey[0], backgroundColor: "transparent", strokeStyle: "dashed" },
      { type: "text", x: 20, y: y + 12, text, fontSize: 20, strokeColor: COLOURS.grey[0] },
    ];
    const steps = [
      box("rectangle", "ask", 160, 45, t("diagrams.tpl.lane.ask"), "blue", 160, 70),
      box("rectangle", "plan", 380, 225, t("diagrams.tpl.lane.plan"), "violet", 160, 70),
      box("rectangle", "review", 600, 45, t("diagrams.tpl.lane.review"), "yellow", 160, 70),
      box("rectangle", "ship", 800, 225, t("diagrams.tpl.lane.ship"), "green", 160, 70),
    ];
    return [
      ...lane("lane-you", 0, t("diagrams.tpl.lane.you")),
      ...lane("lane-agent", 180, t("diagrams.tpl.lane.agent")),
      ...steps,
      ...steps.slice(1).map((step, index) => link(steps[index], step)),
    ];
  }
  return [];
}
