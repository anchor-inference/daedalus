import { Suspense, lazy, useEffect, useLayoutEffect, useRef, useState } from "react";
import { api, type Notification } from "./api";
import { lang, t } from "./i18n";
import { navigate, pathFor } from "./router";
import { Button, Popover, Select } from "./ui/primitives";
import { usePetModel, usePetPreference } from "./ui/pet";
import type { PetPose } from "./petstage";

const PetStage = lazy(() => import("./petstage").then((module) => ({ default: module.PetStage })));
const EMOTIONS = ["calm", "joy", "curious", "focused", "worried", "sleepy", "surprised", "proud", "shy", "sad", "determined", "affectionate"];
const ACTIONS = ["idle", "wave", "nod", "peek", "point", "offer", "focus", "read", "write", "think", "listen", "speak", "reassure", "celebrate", "dance", "float", "stretch", "sip", "sleep"];
const PROPS = ["", "book", "notebook", "pencil", "tablet", "laptop", "calendar", "scroll", "map", "hourglass", "clock", "compass", "magnifier", "gear", "key", "wrench", "crystal", "mug", "teapot", "plant", "lantern", "pillow", "blanket", "palette", "brush", "camera", "music", "star", "microphone", "headphones", "envelope", "plane", "bubble", "bell", "gift", "balloon", "trophy", "heart", "shield"];
const LINES = ["pet.line.hello", "pet.line.tea", "pet.line.here", "pet.line.pause", "pet.line.idea"];
const IDLE_POSES: PetPose[] = [
  { emotion: "joy", action: "wave", prop: "" },
  { emotion: "curious", action: "think", prop: "gear" },
  { emotion: "calm", action: "sip", prop: "mug" },
  { emotion: "focused", action: "read", prop: "book" },
  { emotion: "sleepy", action: "stretch", prop: "" },
];
const random = <T,>(items: T[]): T => items[Math.floor(Math.random() * items.length)];
type Reaction = { line: string; emotion: string; action: string; prop: string };
type Corner = "br" | "bl" | "tr" | "tl";
type Placement = { x: number; y: number; corner: Corner | "custom" };
const mobile = () => window.matchMedia("(max-width: 700px)").matches;
const storageKey = () => `daedalus.pet.position.${mobile() ? "mobile" : "desktop"}`;
const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(value, high));
const dimensions = (activity: string, scale: number) => ({ width: (mobile() ? 130 : activity === "settings" ? 180 : 264) * scale, height: (mobile() ? 170 : activity === "settings" ? 220 : 310) * scale });
const edgeLimit = (activity: string) => mobile() && activity !== "companion" ? 55 : 8;
function savedScale(): number {
  try {
    const value = Number(localStorage.getItem("daedalus.pet.scale"));
    if (Number.isFinite(value) && value >= 0.6 && value <= 1) return value;
  } catch { /* Storage can be unavailable in private mode. */ }
  return 1;
}
function boundPosition(position: Placement, activity: string, scale: number): Placement {
  const { width, height } = dimensions(activity, scale);
  return { ...position, x: clamp(position.x, 8, Math.max(8, window.innerWidth - width - 8)), y: clamp(position.y, 8, Math.max(8, window.innerHeight - height - edgeLimit(activity))) };
}
function cornerPosition(corner: Corner, activity: string, scale: number): Placement {
  const { width, height } = dimensions(activity, scale);
  return boundPosition({ corner, x: corner.endsWith("r") ? window.innerWidth - width - 12 : 12, y: corner.startsWith("b") ? window.innerHeight - height - edgeLimit(activity) : 8 }, activity, scale);
}
function savedPosition(activity: string, scale: number): Placement {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey()) || "null");
    if (saved && Number.isFinite(saved.x) && Number.isFinite(saved.y) && ["br", "bl", "tr", "tl", "custom"].includes(saved.corner)) return boundPosition(saved, activity, scale);
    const oldCorner = localStorage.getItem("daedalus.pet.corner");
    if (["br", "bl", "tr", "tl"].includes(oldCorner || "")) return cornerPosition(oldCorner as Corner, activity, scale);
  } catch { /* Storage can be unavailable in private mode. */ }
  return cornerPosition(mobile() ? "tr" : "br", activity, scale);
}
function savePosition(position: Placement) {
  try { localStorage.setItem(storageKey(), JSON.stringify(position)); } catch { /* Private mode. */ }
}

// The menu is a popover drawn in the page's overlay layer, so the bubble finds it by its class at the
// moment it is placed rather than holding a reference to a box it does not own.
const menuBox = () => document.querySelector<HTMLElement>(".pet-menu");

function placePopup(popup: HTMLElement | null, anchor: HTMLElement | null, fallback: HTMLElement | null, scale: number, obstacle?: HTMLElement | null) {
  if (!popup || !fallback) return;
  const hostRect = fallback.getBoundingClientRect();
  // Voice has no floating canvas, so keep its text near the saved mascot position.
  const voicePoint = hostRect.top + dimensions("companion", scale).height - 50 * scale;
  const rect = anchor ? anchor.getBoundingClientRect() : { left: hostRect.left, right: hostRect.right, width: hostRect.width, top: voicePoint, bottom: voicePoint, height: 0 };
  const width = popup.offsetWidth, height = popup.offsetHeight, gap = 10, margin = 8;
  const hostTop = hostRect.top;
  const centerX = rect.left + rect.width / 2;
  const centerY = rect.top + rect.height / 2;
  // The canvas has transparent padding above the head; use its visible centre as the anchor.
  const top = anchor ? rect.top + Math.min(36, rect.height * 0.16) : rect.top;
  const choices = [
    { x: centerX - width / 2, y: top - height - gap },
    { x: rect.left - width - gap, y: centerY - height / 2 },
    { x: rect.right + gap, y: centerY - height / 2 },
    { x: centerX - width / 2, y: rect.bottom + gap },
  ];
  const placed = choices.map(({ x, y }) => {
    const left = clamp(x, margin, Math.max(margin, window.innerWidth - width - margin));
    const top = clamp(y, margin, Math.max(margin, window.innerHeight - height - margin));
    return { x: left, y: top, displacement: Math.abs(left - x) + Math.abs(top - y) };
  });
  const occupied = obstacle?.getBoundingClientRect();
  const overlap = (x: number, y: number) => occupied ? Math.max(0, Math.min(x + width, occupied.right) - Math.max(x, occupied.left)) * Math.max(0, Math.min(y + height, occupied.bottom) - Math.max(y, occupied.top)) : 0;
  const fitting = placed.find(({ x, y, displacement }, index) => (index !== 0 || hostTop >= height + gap + margin) && displacement <= width / 2 && overlap(x, y) === 0);
  const ranked = placed.map((candidate, index) => ({ candidate, penalty: candidate.displacement * 1000 + overlap(candidate.x, candidate.y) + (index === 0 && hostTop < height + gap + margin ? 1_000_000 : 0) }));
  ranked.sort((a, b) => a.penalty - b.penalty);
  const choice = fitting || ranked[0].candidate;
  popup.style.left = `${choice.x}px`;
  popup.style.top = `${choice.y}px`;
  popup.style.visibility = "visible";
}

export function PetHost({ needsReply, activity }: { needsReply: boolean; activity: string }) {
  const [enabled, setEnabled] = usePetPreference();
  const [model] = usePetModel();
  const [pose, setPose] = useState<PetPose>({ emotion: "calm", action: "idle", prop: "" });
  const [line, setLine] = useState("");
  const [notice, setNotice] = useState<Notification | null>(null);
  const [speaking, setSpeaking] = useState(0);
  const [menu, setMenu] = useState(false);
  const [scale, setScale] = useState(savedScale);
  const [position, setPosition] = useState<Placement>(() => savedPosition(activity, scale));
  const [busy, setBusy] = useState(false);
  const lastReaction = useRef(0);
  const lastActivity = useRef(activity);
  const lineTimer = useRef<number | undefined>(undefined);
  const poseTimer = useRef<number | undefined>(undefined);
  const holdTimer = useRef<number | undefined>(undefined);
  const drag = useRef<{ id: number; x: number; y: number; origin: Placement; moved: boolean } | null>(null);
  const hostRef = useRef<HTMLDivElement>(null);
  const figureRef = useRef<HTMLDivElement>(null);
  const bubbleRef = useRef<HTMLButtonElement>(null);
  const viewport = useRef(mobile());

  const say = (text: string, next: PetPose, entry: Notification | null = null, duration = entry ? 12000 : 7000) => {
    window.clearTimeout(lineTimer.current);
    window.clearTimeout(poseTimer.current);
    setLine(text);
    setNotice(entry);
    setPose(next);
    setSpeaking((n) => n + 1);
    lineTimer.current = window.setTimeout(() => { setLine(""); setNotice(null); }, duration);
    poseTimer.current = window.setTimeout(() => setPose({ emotion: "calm", action: "idle", prop: "" }), Math.max(9000, duration - 1000));
  };
  const generateReaction = async (event: string, manual = false) => {
    if (!model || busy || (!manual && Date.now() - lastReaction.current < 5 * 60_000)) return;
    lastReaction.current = Date.now();
    setBusy(true);
    try {
      const response = await api.post<Reaction>("/api/pet/react", { preset: model, event, lang: lang() });
      if (enabled && response.line) say(response.line, { emotion: response.emotion, action: response.action, prop: response.prop });
    } catch { if (manual) say(t("pet.line.unavailable"), { emotion: "worried", action: "reassure", prop: "lantern" }); }
    finally { setBusy(false); }
  };

  useEffect(() => {
    if (!enabled) return;
    const onNotice = (event: Event) => {
      const entry = (event as CustomEvent<Notification>).detail;
      say(entry.title, { emotion: entry.tone === "error" ? "worried" : entry.needs_you ? "curious" : "joy", action: entry.needs_you ? "point" : "wave", prop: entry.needs_you ? "bell" : "envelope" }, entry);
    };
    window.addEventListener("daedalus:pet-notice", onNotice);
    return () => window.removeEventListener("daedalus:pet-notice", onNotice);
  }, [enabled, model, busy]);
  useEffect(() => {
    const onClear = (event: Event) => {
      const ids = (event as CustomEvent<number[] | number | "all">).detail;
      if (notice && (ids === "all" || ids === notice.id || Array.isArray(ids) && ids.includes(notice.id))) {
        setLine("");
        setNotice(null);
      }
    };
    window.addEventListener("daedalus:pet-clear", onClear);
    return () => window.removeEventListener("daedalus:pet-clear", onClear);
  }, [notice]);
  useEffect(() => {
    if (enabled && !needsReply) say(t("pet.line.hello"), { emotion: "joy", action: "wave", prop: "" }, null, 12000);
  }, [enabled]);
  useEffect(() => {
    if (!enabled) return;
    const timer = window.setInterval(() => {
      const focus = document.activeElement;
      if (document.hidden || line || needsReply || focus instanceof HTMLInputElement || focus instanceof HTMLTextAreaElement || focus instanceof HTMLElement && focus.isContentEditable) return;
      say(t(random(LINES)), random(IDLE_POSES));
    }, 4 * 60_000);
    return () => window.clearInterval(timer);
  }, [enabled, line, needsReply]);
  useEffect(() => {
    if (!enabled || !model) return;
    const timer = window.setInterval(() => { if (!document.hidden && !line && !needsReply) void generateReaction("quiet moment"); }, 10 * 60_000);
    return () => window.clearInterval(timer);
  }, [enabled, model, line, needsReply, busy]);
  useEffect(() => {
    if (enabled && needsReply && !notice) {
      say(t("pet.line.waiting"), { emotion: "curious", action: "listen", prop: "hourglass" }, null, 15000);
      void generateReaction("waiting for operator");
    }
  }, [enabled, needsReply]);
  useEffect(() => {
    if (activity !== lastActivity.current) {
      lastActivity.current = activity;
      if (enabled && model) void generateReaction(`opened ${activity}`);
    }
  }, [activity, enabled, model]);
  useEffect(() => () => { window.clearTimeout(lineTimer.current); window.clearTimeout(poseTimer.current); window.clearTimeout(holdTimer.current); }, []);
  useLayoutEffect(() => {
    if (!enabled) return;
    const update = () => {
      if (viewport.current !== mobile()) {
        viewport.current = mobile();
        setPosition(savedPosition(activity, scale));
      } else setPosition((current) => {
        const next = current.corner === "custom" ? boundPosition(current, activity, scale) : cornerPosition(current.corner, activity, scale);
        return next.x === current.x && next.y === current.y ? current : next;
      });
      placePopup(bubbleRef.current, figureRef.current, hostRef.current, scale, menuBox());
    };
    update();
    window.addEventListener("resize", update);
    return () => window.removeEventListener("resize", update);
  }, [activity, enabled, scale]);
  useLayoutEffect(() => {
    if (!enabled) return;
    const update = () => placePopup(bubbleRef.current, figureRef.current, hostRef.current, scale, menuBox());
    update();
    // The popover is placed by its own layer a frame after it mounts; the bubble steps aside for the
    // box where it stands then, not where it was first drawn.
    const frame = menu ? requestAnimationFrame(() => requestAnimationFrame(update)) : 0;
    const observer = new ResizeObserver(update);
    if (bubbleRef.current) observer.observe(bubbleRef.current);
    return () => { cancelAnimationFrame(frame); observer.disconnect(); };
  }, [enabled, line, notice, menu, position, activity, scale]);
  const startDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    if (event.button !== 0 || menu) return;
    drag.current = { id: event.pointerId, x: event.clientX, y: event.clientY, origin: position, moved: false };
    // Synthetic pointer events in the browser harness have no active pointer to capture.
    try { event.currentTarget.setPointerCapture(event.pointerId); } catch { /* No active pointer. */ }
    if (event.pointerType === "touch") holdTimer.current = window.setTimeout(() => { drag.current = null; setMenu(true); }, 650);
  };
  const moveDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current || event.pointerId !== current.id) return;
    const dx = event.clientX - current.x, dy = event.clientY - current.y;
    if (!current.moved && Math.hypot(dx, dy) < 6) return;
    current.moved = true;
    window.clearTimeout(holdTimer.current);
    setPosition(boundPosition({ x: current.origin.x + dx, y: current.origin.y + dy, corner: "custom" }, activity, scale));
  };
  const endDrag = (event: React.PointerEvent<HTMLDivElement>) => {
    const current = drag.current;
    if (!current || event.pointerId !== current.id) return;
    drag.current = null;
    window.clearTimeout(holdTimer.current);
    if (current.moved) {
      const next = boundPosition({ x: current.origin.x + event.clientX - current.x, y: current.origin.y + event.clientY - current.y, corner: "custom" }, activity, scale);
      setPosition(next);
      savePosition(next);
    }
  };
  if (!enabled) return null;
  return <div ref={hostRef} className={`pet-host ${activity === "settings" ? "in-settings" : ""} ${activity === "companion" ? "in-voice" : ""}`} style={{ left: position.x, top: position.y, "--pet-scale": scale } as React.CSSProperties} aria-label={t("pet.title")}>
    {line && <button ref={bubbleRef} className="pet-bubble" role="status" onClick={() => { if (notice) navigate(pathFor("inbox")); else setLine(""); }}>
      {line}{notice?.body && <small>{notice.body}</small>}
    </button>}
    {activity !== "companion" && <div ref={figureRef} className="pet-figure" role="button" tabIndex={0} aria-label={t("pet.title")}
      onContextMenu={(event) => { event.preventDefault(); setMenu(true); }}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === "ContextMenu" || event.shiftKey && event.key === "F10") { event.preventDefault(); setMenu(true); } }}
      onPointerDown={startDrag}
      onPointerMove={moveDrag}
      onPointerUp={endDrag}
      onPointerCancel={endDrag}>
      <Suspense fallback={null}><PetStage pose={pose} speaking={speaking} /></Suspense>
    </div>}
    {menu && activity !== "companion" && (
      <Popover anchor={figureRef.current} onClose={() => setMenu(false)} label={t("pet.title")} role="dialog" width={300} className="pet-menu">
        <div className="pet-menu-rows">
          <PetChoice label={t("pet.emotion")} value={pose.emotion} options={EMOTIONS.map((id) => ({ id, label: id }))} onChange={(emotion) => setPose({ ...pose, emotion })} />
          <PetChoice label={t("pet.action")} value={pose.action} options={ACTIONS.map((id) => ({ id, label: id }))} onChange={(action) => setPose({ ...pose, action })} />
          <PetChoice label={t("pet.prop")} value={pose.prop} options={PROPS.map((id) => ({ id, label: id || t("pet.none") }))} onChange={(prop) => setPose({ ...pose, prop })} />
          <PetChoice label={t("pet.move")} value={position.corner} options={[...(["br", "bl", "tr", "tl"] as const).map((id) => ({ id: id as string, label: t(`pet.corner.${id}`) })), ...(position.corner === "custom" ? [{ id: "custom", label: t("pet.corner.custom") }] : [])]}
            onChange={(corner) => { if (corner === "custom") return; const next = cornerPosition(corner as Corner, activity, scale); setPosition(next); savePosition(next); }} />
          <label className="pet-row pet-menu-size">
            <span className="pet-row-name">{t("pet.size")}</span>
            <span className="pet-size">
              <input type="range" min="60" max="100" step="5" value={Math.round(scale * 100)} onChange={(event) => {
                const next = Number(event.target.value) / 100;
                setScale(next);
                try { localStorage.setItem("daedalus.pet.scale", String(next)); } catch { /* Private mode. */ }
              }} />
              <output>{Math.round(scale * 100)}%</output>
            </span>
          </label>
        </div>
        <div className="pet-menu-actions">
          <Button size="sm" variant="primary" className="pet-menu-generate" onClick={() => { setMenu(false); void generateReaction("operator asks for a brief greeting", true); }} disabled={!model || busy}>{t("pet.generate")}</Button>
          <Button size="sm" variant="quiet" className="pet-menu-hide" onClick={() => { setEnabled(false); setMenu(false); }}>{t("pet.off")}</Button>
        </div>
      </Popover>
    )}
  </div>;
}

/** One row of the menu: what is chosen, and the list it is chosen from. */
function PetChoice({ label, value, options, onChange }: { label: string; value: string; options: { id: string; label: string }[]; onChange: (id: string) => void }) {
  return (
    <div className="pet-row">
      <span className="pet-row-name">{label}</span>
      <Select label={label} value={value} options={options} onChange={onChange} />
    </div>
  );
}
