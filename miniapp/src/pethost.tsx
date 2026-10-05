import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { api, type Notification } from "./api";
import { lang, t } from "./i18n";
import { navigate, pathFor } from "./router";
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

export function PetHost({ needsReply, activity }: { needsReply: boolean; activity: string }) {
  const [enabled, setEnabled] = usePetPreference();
  const [model] = usePetModel();
  const [pose, setPose] = useState<PetPose>({ emotion: "calm", action: "idle", prop: "" });
  const [line, setLine] = useState("");
  const [notice, setNotice] = useState<Notification | null>(null);
  const [speaking, setSpeaking] = useState(0);
  const [menu, setMenu] = useState(false);
  const [corner, setCorner] = useState<"br" | "bl" | "tr" | "tl">(() => {
    try {
      const value = localStorage.getItem("daedalus.pet.corner");
      if (value === "br" || value === "bl" || value === "tr" || value === "tl") return value;
    } catch { /* private mode */ }
    return window.matchMedia("(max-width: 700px)").matches ? "tr" : "br";
  });
  const [busy, setBusy] = useState(false);
  const lastReaction = useRef(0);
  const lastActivity = useRef(activity);
  const lineTimer = useRef<number | undefined>(undefined);
  const poseTimer = useRef<number | undefined>(undefined);
  const holdTimer = useRef<number | undefined>(undefined);
  const touchStart = useRef<[number, number] | null>(null);

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
  useEffect(() => {
    if (!menu) return;
    const close = (event: PointerEvent) => {
      if (!(event.target as Element).closest(".pet-menu, .pet-figure")) setMenu(false);
    };
    const escape = (event: KeyboardEvent) => { if (event.key === "Escape") setMenu(false); };
    window.addEventListener("pointerdown", close);
    window.addEventListener("keydown", escape);
    return () => { window.removeEventListener("pointerdown", close); window.removeEventListener("keydown", escape); };
  }, [menu]);
  if (!enabled) return null;
  return <div className={`pet-host corner-${corner} ${activity === "settings" ? "in-settings" : ""} ${activity === "voice" ? "in-voice" : ""}`} aria-label={t("pet.title")}>
    {line && <button className="pet-bubble" role="status" onClick={() => { if (notice) navigate(pathFor("inbox")); else setLine(""); }}>
      {line}{notice?.body && <small>{notice.body}</small>}
    </button>}
    {activity !== "voice" && <div className="pet-figure" role="button" tabIndex={0} aria-label={t("pet.title")}
      onContextMenu={(event) => { event.preventDefault(); setMenu(true); }}
      onKeyDown={(event) => { if (event.key === "Enter" || event.key === "ContextMenu" || event.shiftKey && event.key === "F10") { event.preventDefault(); setMenu(true); } }}
      onPointerDown={(event) => { if (event.pointerType === "touch") { touchStart.current = [event.clientX, event.clientY]; holdTimer.current = window.setTimeout(() => setMenu(true), 650); } }}
      onPointerMove={(event) => { if (touchStart.current && Math.hypot(event.clientX - touchStart.current[0], event.clientY - touchStart.current[1]) > 10) window.clearTimeout(holdTimer.current); }}
      onPointerUp={() => { touchStart.current = null; window.clearTimeout(holdTimer.current); }}
      onPointerCancel={() => { touchStart.current = null; window.clearTimeout(holdTimer.current); }}>
      <Suspense fallback={null}><PetStage pose={pose} speaking={speaking} /></Suspense>
    </div>}
    {menu && activity !== "voice" && <div className="pet-menu" role="dialog" aria-label={t("pet.title")}>
      <div className="pet-menu-head"><strong>{t("pet.title")}</strong><button className="pet-menu-close" onClick={() => setMenu(false)} aria-label={t("common.close")}>×</button></div>
      <div className="pet-menu-fields">
      <label>{t("pet.emotion")}<select value={pose.emotion} onChange={(event) => setPose({ ...pose, emotion: event.target.value })}>{EMOTIONS.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
      <label>{t("pet.action")}<select value={pose.action} onChange={(event) => setPose({ ...pose, action: event.target.value })}>{ACTIONS.map((id) => <option key={id} value={id}>{id}</option>)}</select></label>
      <label>{t("pet.prop")}<select value={pose.prop} onChange={(event) => setPose({ ...pose, prop: event.target.value })}>{PROPS.map((id) => <option key={id} value={id}>{id || t("pet.none")}</option>)}</select></label>
      <label>{t("pet.move")}<select value={corner} onChange={(event) => { const next = event.target.value as typeof corner; setCorner(next); try { localStorage.setItem("daedalus.pet.corner", next); } catch { /* private mode */ } }}>
        {(["br", "bl", "tr", "tl"] as const).map((id) => <option key={id} value={id}>{t(`pet.corner.${id}`)}</option>)}
      </select></label>
      </div>
      <div className="pet-menu-actions">
        <button className="pet-menu-generate" onClick={() => { setMenu(false); void generateReaction("operator asks for a brief greeting", true); }} disabled={!model || busy}>{t("pet.generate")}</button>
        <button className="pet-menu-hide" onClick={() => { setEnabled(false); setMenu(false); }}>{t("pet.off")}</button>
      </div>
    </div>}
  </div>;
}
