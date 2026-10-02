// Hero: Daedalus on a dark plinth under a soft cone of light. Every step change is its own small act
// (acts.js) and the new step grows out of the act's object; the stage around him (backdrop.js) takes
// a slightly different accent per step. The camera barely moves; it drifts and pushes in on Start.
import { THREE, createStage, studioLights, Guide, createBubble, wireWizard, reactionFor, reducedMotion, tween, wait, ease } from "./stage3d.js";
import { buildBackdrop } from "./backdrop.js";
import { makeActs } from "./acts.js";

const canvas = document.getElementById("gl");
const zoneEl = document.querySelector(".guide-zone");
// full device pixel ratio up to 2: the stage is mostly soft light, and at 1.5 the floor lines and
// the far windows were visibly resampled on high-density screens
const stage = createStage({ canvas, transparent: true, fov: 28, exposure: 1.1, envIntensity: 0.5, maxDpr: 2, zone: () => zoneEl.getBoundingClientRect() });
const { scene } = stage;
const lights = studioLights(scene, { key: 1.7, rim: 9, hemi: 0.18, fill: 0.5, shadowRadius: 0.9 });

// ---- the stage: a glossy plinth, a faint mint ring, a pool of light on the floor ----
const GROUND = 0.03;
const plinth = new THREE.Mesh(new THREE.CylinderGeometry(0.15, 0.165, GROUND, 72), new THREE.MeshPhysicalMaterial({ color: 0x0b0e0f, roughness: 0.25, metalness: 0.2, clearcoat: 1, clearcoatRoughness: 0.15 }));
plinth.position.y = GROUND / 2; plinth.receiveShadow = true; scene.add(plinth);
const ringMat = new THREE.MeshBasicMaterial({ color: 0x5ee2c4, transparent: true, opacity: 0.8, toneMapped: false });
const ring = new THREE.Mesh(new THREE.TorusGeometry(0.153, 0.0028, 8, 120), ringMat);
ring.rotation.x = Math.PI / 2; ring.position.y = GROUND; scene.add(ring);
const pool = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const g = c.getContext("2d"), r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, "rgba(94,226,196,.22)"); r.addColorStop(0.45, "rgba(94,226,196,.06)"); r.addColorStop(1, "rgba(94,226,196,0)");
  g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  const m = new THREE.Mesh(new THREE.PlaneGeometry(1.6, 1.6), new THREE.MeshBasicMaterial({ map: new THREE.CanvasTexture(c), transparent: true, depthWrite: false }));
  m.rotation.x = -Math.PI / 2; m.position.y = -0.002; scene.add(m); return m;
})();

// ---- the guide and the card he carries ----
const guide = new Guide(stage, { size: 0.1 });
guide.place(new THREE.Vector3(0, GROUND, 0));
const bubble = sideBubble(document.querySelector(".bubble"));

// The bubble hangs beside his head, on the side of the step card when there is room, with a margin
// from every edge, below the corner chrome and clear of the card. It used to sit above the crest,
// which on most windows meant against the top of the page.
function sideBubble(el) {
  const span = el.querySelector("span");
  const head = new THREE.Vector3(), right = new THREE.Vector3(), p = new THREE.Vector3();
  const chrome = Array.from(document.querySelectorAll(".brand, .lang-box"));
  let text = "", shown = false, timer = 0, outTimer = 0;
  const M = 16;
  const proj = (w) => { p.copy(w).project(stage.camera); const { W, H } = stage.size; return { x: (p.x * 0.5 + 0.5) * W, y: (-p.y * 0.5 + 0.5) * H }; };
  stage.onFrame(() => {
    if (!shown) return;
    const z = el.currentCSSZoom || 1;
    const { W, H } = stage.size;
    guide.d.head.getWorldPosition(head);
    right.setFromMatrixColumn(stage.camera.matrixWorld, 0).multiplyScalar(guide.size * 1.25);
    const R = proj(head.clone().add(right)), L = proj(head.clone().sub(right));
    const w = el.offsetWidth * z, h = el.offsetHeight * z;
    const fr = document.querySelector(".frame");
    const f = fr ? fr.getBoundingClientRect() : { left: W, top: H, right: W, bottom: H };
    let top0 = M;
    for (const c of chrome) { const r = c.getBoundingClientRect(); if (r.width) top0 = Math.max(top0, r.bottom + 12); }
    // in a wide window the card is to his right: the bubble may not reach into it
    const limitR = portrait ? W - M : f.left - M;
    const limitB = portrait ? f.top - 12 : H - M;
    // the side toward the card first; the bubble narrows (and wraps) to fit the room it has
    const roomR = limitR - (R.x + 12), roomL = L.x - 12 - M;
    const side = roomR >= 120 || roomR >= roomL ? "r" : "l";
    const room = Math.max(90, side === "r" ? roomR : roomL);
    const maxW = Math.round(Math.min(230 * z, room) / z);
    if (el._maxW !== maxW) { el._maxW = maxW; el.style.maxWidth = maxW + "px"; }
    const left = side === "r" ? R.x + 12 : L.x - 12 - w;
    const top = Math.max(top0, Math.min(R.y - h / 2, limitB - h));
    el.classList.toggle("l", side === "l");
    el.style.setProperty("--ty", `${Math.max(14, Math.min(h - 14, R.y - top)) / z}px`);
    el.style.setProperty("--ox", side === "l" ? "100%" : "0");
    el.style.transform = `translate3d(${Math.round(left / z)}px, ${Math.round(top / z)}px, 0)`;
  });
  function show(fill) {
    clearInterval(timer); clearTimeout(outTimer);
    el.hidden = false; shown = true;
    el.classList.remove("on", "out"); void el.offsetWidth; el.classList.add("on");
    fill();
  }
  return {
    say(s) {
      if (!s) return this.hide();
      if (s === text && shown && !el.classList.contains("out")) return;
      text = s;
      show(() => {
        if (reducedMotion()) { span.textContent = s; return; }
        const words = s.split(" "); let n = 0; span.textContent = "";
        timer = setInterval(() => { n++; span.textContent = words.slice(0, n).join(" "); if (n >= words.length) clearInterval(timer); }, 55);
      });
    },
    // three breathing dots while he waits on something
    think() { text = ""; show(() => { span.innerHTML = '<i class="dots" aria-label="…"><b></b><b></b><b></b></i>'; }); },
    hide() {
      if (!shown) return;
      text = ""; clearInterval(timer);
      el.classList.remove("on"); el.classList.add("out");
      outTimer = setTimeout(() => { el.hidden = true; shown = false; el.classList.remove("out"); }, reducedMotion() ? 0 : 200);
    },
  };
}

// ---- scene dressing and acting ----
const backdrop = buildBackdrop(stage, { THREE, GROUND, reduced: reducedMotion });
stage.onFrame((dt, t) => backdrop.update(dt, t));
let portrait = false, current = "welcome";
const acts = makeActs({ THREE, stage, guide, GROUND, isPortrait: () => portrait });

// A per-step accent in the light: the rim takes a slightly different colour on every step.
const RIM = { welcome: 0x8ff0d6, runs: 0x7fe0e6, model: 0x9fd8ff, limit: 0xffcf9a, telegram: 0xa8ecff, advanced: 0xe8b47a, summary: 0x8ff0d6, start: 0xbfffee };
const rimFrom = new THREE.Color(), rimTo = new THREE.Color();
function accent(id) {
  backdrop.setAccent(id);
  rimFrom.copy(lights.rim.color); rimTo.setHex(RIM[id] || RIM.welcome);
  tween(0.8, (k) => lights.rim.color.lerpColors(rimFrom, rimTo, k));
}

// ---- camera: still, a few degrees of drift per step, and a hint of parallax with the cursor ----
const INDEX = { welcome: 0, runs: 1, model: 2, limit: 3, telegram: 4, advanced: 5, summary: 6, start: 7 };
const ptr = { x: 0, y: 0, sx: 0, sy: 0 };
window.addEventListener("pointermove", (e) => { ptr.x = (e.clientX / window.innerWidth) * 2 - 1; ptr.y = (e.clientY / window.innerHeight) * 2 - 1; backdrop.pointer(ptr.x, ptr.y); }, { passive: true });
function aimCamera(id, snap, push) {
  const i = INDEX[id] || 0;
  // aimed at the middle of figure and plinth together, so the pair sits centred in its zone; the
  // projection centre is the zone's centre, and a vertical line through the aim point stays on it
  const look = new THREE.Vector3(0, (guide.height + GROUND) * 0.5, 0);
  // He and the plinth take a share of the window's height, never more than about 56 % and never
  // much taller than the card beside him; on a phone he fills most of the strip above the card.
  const fr = document.querySelector(".frame");
  const { H } = stage.size;
  const zoneH = stage.zone ? stage.zone.h : H;
  const card = fr ? fr.getBoundingClientRect().height : 560;
  const px = portrait ? zoneH * 0.78 : Math.min(H * 0.56, card * 1.02);
  const fill = Math.min(0.9, (px / zoneH) * (push ? 1.1 : 1));
  const dist = stage.distanceFor(guide.height + GROUND, guide.height * 0.7, fill);
  const az = (portrait ? 0 : 0.14) + 0.09 * Math.sin(i * 1.35), el = 0.1 + 0.025 * Math.cos(i * 1.1);
  const pos = look.clone().add(new THREE.Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el)).multiplyScalar(dist));
  stage.rig.set(pos, look, snap);
  lights.aim(new THREE.Vector3(0, GROUND, 0));
}
const camRight = new THREE.Vector3();
stage.onFrame((dt) => {
  if (reducedMotion()) return;
  const a = 1 - Math.exp(-3 * dt);
  ptr.sx += (ptr.x - ptr.sx) * a; ptr.sy += (ptr.y - ptr.sy) * a;
  if (Math.abs(ptr.sx) + Math.abs(ptr.sy) < 1e-4) return;
  // the camera sways around the aim point, so the guide stays centred and the far layers move
  camRight.setFromMatrixColumn(stage.camera.matrixWorld, 0);
  const d = stage.camera.position.distanceTo(stage.rig.look);
  stage.camera.position.addScaledVector(camRight, -ptr.sx * d * 0.03);
  stage.camera.position.y += ptr.sy * d * 0.015;
  stage.camera.lookAt(stage.rig.look);
});

// ---- step changes: each one is an act ----
const T = () => window.Wizard.t;
let seq = 0, anims = [];
function fly(el, from, pt, opt) {
  if (!el || !el.animate || !pt) return;
  const r = el.getBoundingClientRect();
  // the wizard may be zoomed on a large screen; the translate is in its own pixels
  const z = el.currentCSSZoom || 1;
  const dx = (pt.x - (r.left + r.width / 2)) / z, dy = (pt.y - (r.top + r.height / 2)) / z;
  const small = `translate(${Math.round(dx)}px, ${Math.round(dy)}px) scale(.06)`;
  anims.push(el.animate(from ? [{ transform: small, opacity: 0, filter: "brightness(2)" }, { transform: "none", opacity: 1, filter: "none" }] : [{ transform: "none", opacity: 1 }, { transform: small, opacity: 0 }], opt));
}
const coreScreen = () => { const v = new THREE.Vector3(); guide.d.halo.getWorldPosition(v).project(stage.camera); const { W, H } = stage.size; return { x: (v.x * 0.5 + 0.5) * W, y: (-v.y * 0.5 + 0.5) * H }; };

async function enter(e) {
  const my = ++seq;
  anims.forEach((a) => a.cancel()); anims = [];
  const alive = () => my === seq;
  const old = document.querySelector(".frame .stage .step.leaving");
  bubble.hide();
  accent(e.id);
  aimCamera(e.id, e.dir === 0, e.id === "start");
  if (reducedMotion() || !e.el || e.dir === 0) { acts.place(e.id, e.state); finish(e, my); return; }
  if (e.dir > 0) {
    // forward: the old step folds into him and the new one comes out of the act's object
    e.el.style.animation = "none"; if (old) old.style.animation = "none";
    e.el.style.opacity = "0";
    fly(old, false, coreScreen(), { duration: 300, easing: "cubic-bezier(.5,0,.8,.4)", fill: "forwards" });
  }
  // back: the shared slide from the left plays, and he does his own backward move
  await acts.play(e.id, e.state, e.dir, (pt) => {
    if (e.dir <= 0) return;
    e.el.style.animation = ""; e.el.style.opacity = "";
    if (pt) fly(e.el, true, pt, { duration: 420, easing: "cubic-bezier(.2,.9,.25,1.05)", fill: "backwards" });
  }, alive);
  if (!alive()) return;
  e.el.style.animation = ""; e.el.style.opacity = "";
  finish(e, my);
}
function finish(e, my) {
  if (my !== seq) return;
  if (e.id !== "start") acts.glanceAtCard(1.2);
  bubble.say(T()("say." + e.id));
}

function onResize({ W, H }) {
  portrait = W / H < 1 || W <= 720;
  if (window.Wizard) aimCamera(current, true, current === "start");
}

// Whether a typed value now looks right: the moment it does, he gives a thumbs-up.
const VALID = {
  key: (v) => String(v || "").trim().length >= 12,
  "local.url": (v) => /^https?:\/\/[^\s/]+/i.test(String(v || "")),
  "tg.token": (v) => /^\d{5,}:[\w-]{10,}$/.test(String(v || "")),
  "tg.owner": (v) => /^\d{5,}$/.test(String(v || "")),
};
const wasValid = {};
function checkTyped(path, S) {
  const kind = path.indexOf("keys.") === 0 ? "key" : path;
  const test = VALID[kind];
  if (!test) return false;
  const val = path.split(".").reduce((o, k) => (o == null ? o : o[k]), S);
  const ok = test(val);
  const flipped = ok && !wasValid[path];
  wasValid[path] = ok;
  acts.lean();
  if (flipped && !reducedMotion()) acts.thumbsUp();
  return true;
}

wireWizard({
  onMount() {},
  onStep(e) { current = e.id; if (e.id === "start") guide.mood.glow = guide.moodTo.glow = 1; enter(e); },
  onChange(path, S) {
    checkTyped(path, S);
    if (path === "mode") acts.tap(S.mode);
    const r = reactionFor(path, S);
    if (!r) return;
    if (r.say) bubble.say(T()(r.say));
    if (r.mood === "nod") guide.nod();
    else if (r.mood === "happy") { guide.happy(); pulseRing(); acts.thumbsUp(); }
    else if (r.mood === "worry") guide.worried();
    else if (r.mood === "think") { guide.feel("shrug", 0.6, 0.3); bubble.think(); }
  },
  onInvalid() { guide.worried(); bubble.say(T()("say.invalid")); },
  onLaunch(e) {
    // the core brightens as the agent wakes, the ring with it
    tween(0.8, (k) => { guide.mood.glow = guide.moodTo.glow = 1.6 + e.p * 1.2 * k; ringMat.opacity = 0.8 + 0.2 * e.p; });
    if (e.i === 0) { bubble.think(); guide.feel("shrug", 0.5, 0.2); }
    if (e.i === 2) bubble.say(T()("say.start"));
  },
  onReady() { guide.happy(2.4); pulseRing(); bubble.say(T()("say.ready")); },
  onOpen() {
    bubble.say(T()("say.bye"));
    guide.wave(1.2);
    wait(reducedMotion() ? 0 : 0.7).then(() => { bubble.hide(); return guide.flyTo(new THREE.Vector3(0.5, 3.4, -1.2), { high: 0.3, dur: 1.8, curve: (k) => k * k }); });
  },
  onLang() { bubble.say(T()("say." + current)); },
});
function pulseRing() { tween(0.9, (k, raw) => { ringMat.opacity = 0.8 + 0.2 * Math.sin(raw * Math.PI); ring.scale.setScalar(1 + 0.12 * Math.sin(raw * Math.PI)); }).then(() => ring.scale.setScalar(1)); }

stage.start();
onResize(stage.size);
window.addEventListener("resize", () => onResize(stage.size));
