// The studio's 3D stage: a plinth, three mascots built from the brand's parts (chibi2.js), and a
// frame loop that poses whichever is shown. The models are posed here rather than through the
// setup wizard's Guide, whose own per-frame pose (and its turn toward the camera) the first
// version fought by overwriting it afterwards; that layering is what left heads tilted and props
// floating at fixed points of the world.
import { THREE, createStage, studioLights, contactShadow, reducedMotion } from "../desktop/ui/assets/setup/stage3d.js";
import { makeDaedalus, makeChibi, makeMascot } from "../desktop/ui/assets/setup/chibi2.js";
import { FaceRig, insideHead } from "./face.js";
import { makeProp, disposeProp, companionFor } from "./props.js";
import { bodyPose, headPose, blend, breathPeriod, reach, rotate, gripOf, writeTip, smooth, clamp, TAU } from "./motion.js";

const PLINTH = 0.545; // the height of the plinth's top
const EMOTIONS = ["calm", "joy", "curious", "focused", "worried", "sleepy", "surprised", "proud", "shy", "sad", "determined", "affectionate"];
// a still pose for each action under reduced motion: the moment that says what the action is
const KEY_TIME = { idle: 2, wave: 0.6, nod: 0.25, peek: 2.2, point: 1.2, offer: 2, focus: 6, read: 1, write: 1, think: 1, listen: 1, speak: 0.5, reassure: 2, celebrate: 0.5, dance: 0.3, float: 2, stretch: 1.8, sip: 2.8, sleep: 2 };
const BODY_PROFILE = [[0.0, 0.5], [0.3, 0.52], [0.52, 0.6], [0.66, 0.78], [0.72, 1.0], [0.7, 1.25], [0.6, 1.5], [0.44, 1.72], [0.28, 1.84], [0.0, 1.88]];
function bodyRadius(y) { // the lathe profile of chibi2.js's body (its mesh sits 0.1 up), linearly
  const v = y - 0.1;
  for (let i = 1; i < BODY_PROFILE.length; i++) {
    const [r0, y0] = BODY_PROFILE[i - 1], [r1, y1] = BODY_PROFILE[i];
    if (v >= y0 && v <= y1) return r0 + (r1 - r0) * (v - y0) / (y1 - y0);
  }
  return -1;
}

export class MascotStage {
  constructor(canvas, options = {}) {
    this.canvas = canvas;
    this.stage = createStage({ canvas, fitCanvas: true, transparent: true, fov: 30, maxDpr: options.compact ? 1 : 1.5, maxFps: options.compact ? 24 : undefined, exposure: 1.06, envIntensity: 0.6 });
    this.scene = this.stage.scene;
    this.lights = studioLights(this.scene, { target: new THREE.Vector3(0, 2.0, 0), key: 2.4, rim: 8, fill: 0.9, hemi: 0.42, shadowRadius: 3 });
    if (options.compact) this.lights.key.shadow.mapSize.set(512, 512);
    // a low warm light from the front, so the bronze and the face read warm and close
    const warm = new THREE.PointLight(0xffc89a, 1.1, 9, 2); warm.position.set(-1.2, 1.3, 3.2); this.scene.add(warm);
    this.buildPlinth();

    const make = {
      daedalus: () => ({ model: makeDaedalus(0.8), size: 0.8, kind: "body" }),
      chibi: () => ({ model: makeChibi(0.62), size: 0.62, kind: "body" }),
      head: () => ({ model: makeMascot(0.92), size: 0.92, kind: "head", hoverY: 1.6 }),
    };
    this.variants = {};
    for (const [id, f] of Object.entries(make)) {
      if (options.compact && id !== "daedalus") continue;
      const V = f();
      V.id = id;
      const m = V.model;
      m.root.position.y = PLINTH;
      if (m.head) m.head.rotation.order = "YXZ";
      this.scene.add(m.root);
      V.face = new FaceRig(m);
      // things on the plinth or in the air beside the mascot live in its own units, unsquashed
      V.ground = new THREE.Group(); V.ground.scale.setScalar(V.size); m.root.add(V.ground);
      V.hover = new THREE.Group(); V.hover.scale.setScalar(V.size); m.root.add(V.hover);
      V.shadow = contactShadow(V.kind === "head" ? 2.6 * V.size : 2.4 * V.size); V.shadow.position.y = 0.006; m.root.add(V.shadow);
      if (V.kind === "body") {
        V.armBase = m.arms.map((a) => a.position.clone());
        V.wingBase = m.wings.map((w) => ({ s: w.wg.scale.clone() }));
      } else {
        m.glow.material.opacity = 0.16;
      }
      V.pose = null;
      this.variants[id] = V;
    }
    this.mascot = "daedalus";
    this.emotion = "calm";
    this.action = "idle";
    this.prop = "";
    this.voiceLevel = 0;
    this.weights = Object.fromEntries(EMOTIONS.map((e) => [e, e === "calm" ? 1 : 0]));
    this.items = { 1: null, 2: null };
    this.leaving = [];
    this.actionSince = 0;
    this.from = null; this.blendStart = 0; this.blendDur = 0.55; this.changed = { 1: false, 2: false };
    this.breathPh = 0; this.flapPh = 0;
    this.turn = 0; this.turnTo = 0;
    this.frozen = null;
    this.framing = "full";
    this.trail = new Trail(this.scene);
    this.tmp = new THREE.Vector3(); this.tmp2 = new THREE.Vector3(); this.tmpQ = new THREE.Quaternion(); this.tmpE = new THREE.Euler();
    this.bindDrag();
    this.stage.onFrame((dt, t) => this.update(dt, t));
    this.onResize = () => this.frame(true);
    window.addEventListener("resize", this.onResize);
    // the canvas also changes size when the layout does (a phone's toolbar, the library opening)
    this.observer = new ResizeObserver(() => { this.stage.measure(); this.frame(true); });
    this.observer.observe(canvas);
    this.setMascot("daedalus");
    this.stage.start();
    this.frame(true);
  }

  buildPlinth() {
    const base = new THREE.MeshPhysicalMaterial({ color: 0x223138, metalness: 0.45, roughness: 0.42, clearcoat: 0.4 });
    const felt = new THREE.MeshPhysicalMaterial({ color: 0x2c4044, roughness: 0.92, sheen: 0.6, sheenColor: new THREE.Color(0x6fa79a) });
    const ring = new THREE.MeshBasicMaterial({ color: 0x6fe8c8, toneMapped: false });
    const add = (geo, m, y, recv = true) => { const x = new THREE.Mesh(geo, m); x.position.y = y; x.receiveShadow = recv; this.scene.add(x); return x; };
    add(new THREE.CylinderGeometry(1.95, 2.06, 0.44, 96), base, 0.22);
    add(new THREE.TorusGeometry(1.96, 0.022, 10, 120), ring, 0.47).rotation.x = Math.PI / 2;
    add(new THREE.CylinderGeometry(1.92, 1.95, 0.08, 96), felt, 0.505);
    const inlay = add(new THREE.RingGeometry(1.42, 1.45, 96), new THREE.MeshBasicMaterial({ color: 0x6fe8c8, transparent: true, opacity: 0.22, toneMapped: false, depthWrite: false }), PLINTH + 0.003, false);
    inlay.rotation.x = -Math.PI / 2;
    const outer = add(new THREE.TorusGeometry(2.3, 0.012, 8, 120), new THREE.MeshBasicMaterial({ color: 0x4bd8bd, transparent: true, opacity: 0.35, toneMapped: false }), 0.02, false);
    outer.rotation.x = Math.PI / 2;
    // motes drifting up in the light, like dust in a warm room
    const n = 34, pos = new Float32Array(n * 3);
    this.motes = { n, pos, seed: Array.from({ length: n }, (_, i) => [((i * 2.39996) % TAU), 1.4 + ((i * 0.37) % 1.2), (i * 0.618) % 1]) };
    const geo = new THREE.BufferGeometry(); geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    const dot = (() => { const c = document.createElement("canvas"); c.width = c.height = 32; const g = c.getContext("2d"); const r = g.createRadialGradient(16, 16, 0, 16, 16, 16); r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(1, "rgba(255,255,255,0)"); g.fillStyle = r; g.fillRect(0, 0, 32, 32); return new THREE.CanvasTexture(c); })();
    this.motes.points = new THREE.Points(geo, new THREE.PointsMaterial({ size: 0.07, map: dot, color: 0xbff7e6, transparent: true, opacity: 0.55, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false }));
    this.scene.add(this.motes.points);
  }

  bindDrag() {
    let x0 = null, id = null;
    this.canvas.addEventListener("pointerdown", (e) => { x0 = e.clientX; id = e.pointerId; this.canvas.setPointerCapture(id); this.canvas.classList.add("dragging"); });
    this.canvas.addEventListener("pointermove", (e) => { if (x0 === null) return; this.turnTo = clamp(this.turnTo + (e.clientX - x0) * 0.009, -1.25, 1.25); x0 = e.clientX; });
    const end = () => { x0 = null; this.canvas.classList.remove("dragging"); };
    this.canvas.addEventListener("pointerup", end);
    this.canvas.addEventListener("pointercancel", end);
    this.canvas.addEventListener("dblclick", () => this.resetCamera());
  }

  // ---- what the page asks for ----
  setMascot(id) {
    if (!this.variants[id]) return;
    this.mascot = id;
    for (const [name, V] of Object.entries(this.variants)) V.model.root.visible = name === id;
    this.rebuildItems(true);
    this.from = null;
    this.popAt = this.now();
    this.frame();
  }
  setEmotion(id) { if (EMOTIONS.includes(id)) this.emotion = id; }
  setAction(id) {
    if (id === this.action) return;
    this.beginBlend();
    this.action = id;
    this.actionSince = this.now();
    // long enough for an arm to travel from one pose to the next without a lunge
    this.blendDur = id === "float" || id === "sleep" || id === "stretch" || this.prevAction === "float" ? 0.95 : 0.7;
    this.prevAction = id;
    this.rebuildItems();
  }
  setProp(id) {
    if (id === this.prop) return;
    this.beginBlend();
    this.prop = id || "";
    this.rebuildItems();
  }
  setVoiceLevel(n) { this.voiceLevel = clamp(n, 0, 1); }
  dispose() {
    this.observer.disconnect();
    window.removeEventListener("resize", this.onResize);
    this.stage.dispose();
  }
  // The mascot says a line: the mouth moves for about that long, unless it is asleep or drinking.
  talk(seconds) { this.talkFrom = this.now(); this.talkUntil = this.talkFrom + seconds; }
  setFraming(mode) { if (mode !== this.framing) { this.framing = mode; this.frame(); } }
  resetCamera() { this.turnTo = 0; this.frame(); }
  now() { return this.frozen != null ? this.frozen : this.stage.time(); }

  // the pose shown last becomes the start of a blend into whatever comes next, so a change is
  // a movement and never a jump
  beginBlend() {
    const V = this.variants[this.mascot];
    if (!V.pose) return;
    this.from = { ...V.pose };
    this.blendStart = this.now();
  }

  // the items to show for the prop and the action (the chosen prop and, sometimes, a companion)
  rebuildItems(force) {
    const V = this.variants[this.mascot];
    const want = { 1: this.prop, 2: companionFor(this.action, this.prop) };
    for (const n of [1, 2]) {
      const cur = this.items[n];
      if (!force && cur && cur.id === want[n]) continue;
      if (cur) this.retire(cur);
      this.items[n] = null;
      if (want[n]) {
        const built = makeProp(want[n], V.kind);
        if (!built) continue;
        const item = { id: want[n], spec: built.spec, root: built.root, born: this.now(), variant: V.id };
        this.items[n] = item;
        this.scene.add(built.root);
        this.changed[n] = true;
      }
    }
  }
  retire(item) {
    const r = item.root;
    if (!r.parent) return;
    r.updateMatrixWorld(true);
    this.scene.attach(r);
    this.leaving.push({ root: r, at: this.now(), scale: r.scale.clone() });
  }

  // Camera: frames the whole figure, the upper body for the voice, or the face for emotions.
  frame(snap) {
    const V = this.variants[this.mascot];
    const { W, H } = this.stage.size;
    const aspect = Math.max(0.3, W / Math.max(1, H));
    const s = V.size, head = V.kind === "head";
    const headY = head ? PLINTH + V.hoverY : PLINTH + 2.72 * s;
    const box = {
      full: { cy: 2.2, h: 4.95, w: 4.5 },
      medium: { cy: head ? headY - 0.15 : PLINTH + 2.15 * s, h: head ? 3.1 : 3.5 * s + 0.35, w: 3.6 },
      portrait: { cy: headY + 0.12 * s, h: head ? 2.5 : 2.55 * s + 0.25, w: 2.7 * s + 0.3 },
    }[this.framing] || { cy: 2.2, h: 4.95, w: 4.5 };
    const t = Math.tan(THREE.MathUtils.degToRad(this.stage.camera.fov / 2));
    const dist = Math.max(box.h / (2 * t), box.w / (2 * t * aspect));
    const az = 0.3, el = this.framing === "portrait" ? 0.07 : 0.14;
    this.az = az;
    const look = new THREE.Vector3(0, box.cy, 0);
    const pos = new THREE.Vector3(Math.sin(az) * Math.cos(el), Math.sin(el), Math.cos(az) * Math.cos(el)).multiplyScalar(dist).add(look);
    this.stage.rig.set(pos, look, snap);
  }

  // ---- the frame ----
  update(dt, clock) {
    const still = reducedMotion();
    const now = this.frozen != null ? this.frozen : clock;
    // frozen for a check: a few long steps first, so moods and fades arrive where they are going
    if (this.frozen != null) { dt = this.settle > 0 ? 0.5 : 1 / 60; this.settle = Math.max(0, (this.settle || 0) - 1); }
    const V = this.variants[this.mascot], m = V.model;
    // moods fade into each other
    const kw = still ? 1 : 1 - Math.exp(-dt * 3.2);
    for (const e of EMOTIONS) this.weights[e] += ((e === this.emotion ? 1 : 0) - this.weights[e]) * kw;
    this.breathPh += dt * TAU / breathPeriod(this.weights);
    this.turn += (this.turnTo - this.turn) * (still ? 1 : 1 - Math.exp(-dt * 8));
    const t = still ? 2 : now;
    const e = still ? KEY_TIME[this.action] ?? 1 : now - this.actionSince;
    const c = {
      t, e, still, breath: still ? 0 : Math.sin(this.breathPh), voice: this.voiceLevel, action: this.action,
      item: this.items[1] && this.items[1].spec, comp: this.items[2] && this.items[2].spec, variant: V.kind,
      itemAt: this.itemAt(V),
    };
    let P = V.kind === "body" ? bodyPose(this.action, c, this.weights) : headPose(this.action, c, this.weights);
    if (this.from && !still) {
      const k = smooth((now - this.blendStart) / this.blendDur);
      if (k >= 1) this.from = null;
      else {
        // a new item appears in its place rather than flying in from where nothing was, and a
        // hand that held the item it replaced lets go instead of reaching for the newcomer
        for (const n of [1, 2]) if (this.changed[n]) {
          for (const key of ["x", "y", "z", "rx", "ry", "rz"]) this.from[`p${n}${key}`] = P[`p${n}${key}`];
          if (this.from.G0 === n) { this.from.G0 = 0; this.from.g0 = 0; }
          if (this.from.G1 === n) { this.from.G1 = 0; this.from.g1 = 0; }
        }
        P = blend(this.from, P, k);
      }
    } else this.from = null;
    this.changed = { 1: this.changed[1] && !!this.from, 2: this.changed[2] && !!this.from };
    if (!still && this.talkUntil > now && this.action !== "sleep" && this.action !== "sip" && this.action !== "speak") {
      const k = Math.min(1, (this.talkUntil - now) / 0.3, (now - this.talkFrom) / 0.15);
      const syllable = Math.max(0, Math.sin(now * 11) * 0.55 + Math.sin(now * 4.3 + 1) * 0.45);
      P.speak = Math.max(P.speak, syllable * 0.7 * k);
    }
    V.pose = P;
    if (V.kind === "body") this.applyBody(V, P, dt, now, still);
    else this.applyHead(V, P, dt, now, still);
    this.placeItems(V, P, c, now, still);
    if (V.kind === "body") this.gripHands(V, P);
    // the face last, once the head is where it will be drawn
    m.root.updateMatrixWorld(true);
    const look = this.lookFor(V, P);
    V.face.update(dt, now, {
      emotion: this.action === "sleep" && this.emotion !== "sleepy" ? "sleepy" : this.emotion, still,
      close: P.close, open: P.open, smile: P.smile, round: P.round, speak: P.speak, blush: P.blush,
      gazeX: P.gx, gazeY: P.gy, gazeK: P.gk, look: look.dir, lookK: look.k, magnify: P.magnify,
      noBlink: this.action === "sleep", emote: P.emote,
    });
    this.updateLeaving(now);
    this.updateMotes(now, still);
    this.trail.update(this.items, still, this.stage.camera);
    const pop = this.popAt != null ? smooth((now - this.popAt) / 0.35) : 1;
    m.root.scale.setScalar(still ? 1 : 0.92 + 0.08 * pop);
  }

  applyBody(V, P, dt, now, still) {
    const m = V.model, s = V.size;
    m.root.rotation.y = this.az * 0.72 + this.turn + P.yaw;
    m.s.position.set(0, P.lift * s, 0);
    const sq = P.squash;
    m.s.scale.set(s / Math.sqrt(sq), s * sq, s / Math.sqrt(sq));
    // the body leans and rolls about the hips, not about the soles
    const hip = 0.75, cl = Math.cos(P.lean), sl = Math.sin(P.lean), cr = Math.cos(P.roll), sr = Math.sin(P.roll);
    m.bob.position.set(P.bx + hip * sr, P.by + hip - hip * cl * cr, -hip * sl);
    m.bob.rotation.set(P.lean, 0, P.roll);
    m.head.rotation.set(P.hx, P.hy, P.hz);
    m.arms.forEach((a, i) => {
      const b = V.armBase[i];
      a.position.set(b.x + (i ? P.s1x : P.s0x), b.y + (i ? P.s1y : P.s0y), b.z + (i ? P.s1z : P.s0z));
      a.rotation.set(i ? P.a1x : P.a0x, 0, i ? P.a1z : P.a0z);
      const st = i ? P.a1s : P.a0s;
      a.children[0].scale.set(1, st, 1);
      a.children[1].position.y = -0.62 * st;
    });
    m.legs.forEach((l, i) => l.rotation.set(i ? P.l1x : P.l0x, 0, i ? P.l1z : P.l0z));
    const fold = this.items[1] && this.items[1].spec.foldWings ? 0.55 : 1;
    this.flapPh += dt * (5 + 16 * clamp(P.flap, 0, 1));
    m.wings.forEach((w, i) => {
      const sx = w.sx, sp = P.wing + (i ? P.wing1 : P.wing0);
      w.wg.rotation.set(0, sx * (0.6 - 0.25 * sp), sx * (0.35 + 0.42 * sp));
      w.wg.scale.copy(V.wingBase[i].s).multiplyScalar(fold);
      const fl = still ? 0 : P.flap * 0.55 * Math.sin(this.flapPh + i * 0.4);
      w.fs.forEach((pv, k) => { pv.rotation.z = (0.5 - k * 0.42) * (1 + 0.25 * sp); pv.rotation.y = fl * (1 - k * 0.15) + 0.1 * sp; pv.rotation.x = -0.12 * k; });
    });
    if (m.halo) {
      const glow = 0.5 + 0.5 * this.weights.joy + 0.4 * this.weights.determined - 0.3 * this.weights.sleepy - 0.25 * this.weights.sad;
      const pulse = still ? 1 : 0.8 + 0.2 * Math.sin(now * (this.action === "listen" ? 4 : 2.2));
      // listening, the core in the chest breathes with the voice it hears
      const hear = this.action === "listen" ? 0.2 + 0.5 * this.voiceLevel : 0;
      m.haloM.opacity = (0.32 + 0.3 * glow) * pulse + hear;
      m.halo.scale.setScalar(0.62 + 0.28 * glow * pulse);
    }
    if (m.ledM && !m.halo) { const blink = still ? 1 : 0.75 + 0.25 * Math.sin(now * 3); m.ledCoreM.opacity = 0.55 * blink; }
    V.shadow.scale.setScalar(1 / (1 + P.lift * 1.4));
    V.shadow.material.opacity = 0.62 / (1 + P.lift * 1.8);
  }

  applyHead(V, P, dt, now, still) {
    const m = V.model, s = V.size;
    m.root.rotation.y = this.az * 0.72 + this.turn + P.yaw;
    m.s.position.set(P.bx * s, V.hoverY + P.lift * s, 0);
    const sq = P.squash;
    m.s.scale.set(s / Math.sqrt(sq), s * sq, s / Math.sqrt(sq));
    m.head.rotation.set(P.hx, P.hy, P.hz);
    V.hover.position.copy(m.s.position);
    this.flapPh += dt * (5 + 16 * clamp(P.flap, 0, 1));
    m.wings.forEach((w, i) => {
      const sp = P.wing + (i ? P.wing1 : P.wing0);
      w.wg.rotation.set(0, 0, w.sx * 0.5 * sp);
      const fl = still ? 0 : (0.22 + 0.4 * clamp(P.flap, 0, 1)) * Math.sin(this.flapPh + i * 0.3);
      w.fs.forEach((pv, k) => { pv.rotation.y = fl * (1 - k * 0.12) - 0.2; pv.rotation.x = -0.08 * k; pv.rotation.z = (0.62 - k * 0.36) * (1 + 0.15 * sp); });
    });
    m.glow.material.opacity = 0.12 + 0.08 * (still ? 1 : Math.sin(now * 2.2) * 0.5 + 0.5);
    m.glow.position.y = -1.3 - (V.hoverY + P.lift * s) / s + 0.02 / s;
    m.glow.scale.setScalar(1 / (1 + Math.max(0, P.lift)));
    V.shadow.scale.setScalar(0.8 / (1 + Math.max(0, P.lift) * 0.8));
    V.shadow.material.opacity = 0.35;
  }

  // Where the chosen item stands or flies, in the mascot's own units, for an action that points
  // at it or watches it; null for things in the hands.
  itemAt(V) {
    const item = this.items[1];
    if (!item || (item.spec.mount !== "ground" && item.spec.mount !== "float")) return null;
    const r = item.root, d = r.userData.dart || r.userData.notes && r.userData.notes[0];
    const p = r.position;
    if (!r.parent) return null;
    if (d) return [p.x + d.position.x, p.y + d.position.y, p.z + d.position.z];
    return [p.x, p.y + (item.spec.mount === "ground" ? 0.45 : 0), p.z];
  }

  // Puts each item where its mount says: in the body's space at the pose's placement for things
  // held, on the plinth, in the air, on the head.
  placeItems(V, P, c, now, still) {
    const m = V.model;
    for (const n of [1, 2]) {
      const item = this.items[n];
      if (!item) continue;
      const { spec, root } = item;
      const age = still ? 1 : clamp((now - item.born) / 0.38, 0, 1);
      const pop = still ? 1 : 1 + 0.12 * Math.sin(age * Math.PI) - (1 - smooth(age * 1.4));
      const base = spec.scale || 1;
      let parent, mirror = false;
      if (spec.mount === "hand" || spec.mount === "both") {
        parent = V.kind === "body" ? m.bob : V.hover;
        root.position.set(P[`p${n}x`], P[`p${n}y`], P[`p${n}z`]);
        root.rotation.set(P[`p${n}rx`], P[`p${n}ry`], P[`p${n}rz`]);
        mirror = n === 1 ? P.m1 : P.m2;
      } else if (spec.mount === "ground") {
        parent = V.ground;
        const g = spec.ground[V.kind] || spec.ground.body;
        root.position.set(g[0], g[1], g[2]); root.rotation.set(0, g[3] || 0, 0);
      } else if (spec.mount === "float") {
        parent = V.kind === "body" ? V.ground : V.hover;
        const f = spec.float[V.kind] || spec.float.body;
        root.position.set(f[0], f[1] + (still ? 0 : 0.05 * Math.sin(now * 1.3 + n)), f[2]);
        // a thought, a speech bubble, notes: they go where the head goes (a float, a lean, a hop)
        if (V.kind === "body" && spec.follow) root.position.add(this.followOffset(V, spec.follow));
        root.rotation.set(0, -(this.az * 0.72 + this.turn) * 0.5, 0);
      } else if (spec.mount === "head") {
        parent = m.head;
        const h = spec.head; root.position.set(h[0], h[1], h[2]); root.rotation.set(h[3], h[4], h[5]);
      } else {
        parent = V.kind === "body" ? m.bob : m.head;
        root.position.set(0, 0, 0); root.rotation.set(0, 0, 0);
      }
      if (root.parent !== parent) parent.add(root);
      root.scale.set(base * pop * (mirror ? -1 : 1), base * pop, base * pop);
      root.visible = pop > 0.01;
      if (spec.tick) {
        const tip = (this.action === "focus" || this.action === "write") ? writeTip(c.e, still) : null;
        const ph = c.e % 12;
        spec.tick(root, still ? 2 : now, { still, action: this.action, e: c.e, variant: V.kind, tip, writing: this.action === "write" || (ph > 4.2 && ph < 8.2), ringing: this.action === "wave" });
      }
    }
  }

  // How far the head (or the body) is from where it rests, in the mascot's own units.
  followOffset(V, what) {
    const m = V.model;
    m.root.updateMatrixWorld(true);
    const local = V.ground.worldToLocal((what === "head" ? m.head : m.bob).getWorldPosition(this.tmp2));
    if (what === "head") local.y -= 2.72;
    return local;
  }

  // Hands that hold an item are solved onto its grips after the blend, so even halfway through
  // a change of action a held thing stays in the hand.
  gripHands(V, P) {
    const Q = { ...P };
    for (const i of [0, 1]) {
      const g = i ? P.g1 : P.g0, n = i ? P.G1 : P.G0;
      if (!(g > 0.001) || !n) continue;
      const item = this.items[n];
      if (!item || !item.spec.grips) continue;
      // gripOf mirrors the grip for a hand that has none of its own, as holdOne did, and the
      // mesh is drawn mirrored to match, so the same rotation lands on the drawn grip
      const w = rotate(gripOf(item.spec, i), P[`p${n}rx`], P[`p${n}ry`], P[`p${n}rz`]);
      const x = P[`p${n}x`] + w[0], y = P[`p${n}y`] + w[1], z = P[`p${n}z`] + w[2];
      reach(Q, i, x, y, z);
      const keys = i ? ["a1x", "a1z", "a1s"] : ["a0x", "a0z", "a0s"];
      for (const k of keys) P[k] += (Q[k] - P[k]) * g;
    }
    const m = V.model;
    m.arms.forEach((a, i) => {
      a.rotation.set(i ? P.a1x : P.a0x, 0, i ? P.a1z : P.a0z);
      const st = i ? P.a1s : P.a0s;
      a.children[0].scale.set(1, st, 1);
      a.children[1].position.y = -0.62 * st;
    });
  }

  // Where the eyes look: at the point the action names, at you, or where the mood glances.
  lookFor(V, P) {
    const m = V.model, head = m.head;
    let target = null, k = 0;
    if (P.lookAt && V.kind === "body") { target = m.bob.localToWorld(this.tmp.set(P.lookAt[0], P.lookAt[1], P.lookAt[2])); k = 1; }
    else {
      const watch = this.items[1] && this.items[1].spec.watch && (this.action === "idle" || this.action === "point" || this.action === "listen" || this.action === "nod");
      if (watch) {
        const r = this.items[1].root, d = r.userData.dart;
        target = (d || r).getWorldPosition(this.tmp); k = 0.9;
      } else if (P.cam > 0.01) { target = this.tmp.copy(this.stage.camera.position); k = P.cam; }
    }
    if (!target) return { dir: null, k: 0 };
    const local = head.worldToLocal(this.tmp2.copy(target));
    const eyes = local.sub(this.tmp.set(0, 0.03, 0.8)).normalize();
    return { dir: eyes, k };
  }

  updateLeaving(now) {
    this.leaving = this.leaving.filter((x) => {
      const k = (now - x.at) / 0.24;
      if (k >= 1 || reducedMotion()) { x.root.removeFromParent(); disposeProp(x.root); return false; }
      x.root.scale.copy(x.scale).multiplyScalar(1 - smooth(k));
      return true;
    });
  }

  updateMotes(now, still) {
    const M = this.motes;
    for (let i = 0; i < M.n; i++) {
      const [a, r, ph] = M.seed[i];
      const k = still ? ph : (now * 0.035 + ph) % 1;
      M.pos[i * 3] = Math.cos(a + k * 0.6) * (r + 0.25 * Math.sin(k * 7 + i));
      M.pos[i * 3 + 1] = 0.6 + k * 3.8;
      M.pos[i * 3 + 2] = Math.sin(a + k * 0.6) * (r + 0.25 * Math.sin(k * 7 + i)) * 0.8;
    }
    M.points.geometry.attributes.position.needsUpdate = true;
    M.points.material.opacity = 0.5;
  }

  // ---- for the browser checks ----
  freeze(t) {
    this.frozen = t == null ? null : t;
    this.settle = t == null ? 0 : 8;
    if (t == null) return;
    // a frozen frame shows where things arrive: no blend, no prop still growing or leaving
    this.from = null; this.popAt = null;
    for (const n of [1, 2]) if (this.items[n]) this.items[n].born = -1e9;
    this.leaving.forEach((x) => { x.root.removeFromParent(); disposeProp(x.root); });
    this.leaving = [];
  }
  // Moves a frozen clock on by dt without ending a blend, so a check can walk through a change
  // of action frame by frame.
  step(dt) { if (this.frozen == null) this.frozen = this.stage.time(); this.frozen += dt; this.settle = 0; }
  // How deep each item's surface goes into the mascot's head or body (in its own units).
  clipReport() {
    const V = this.variants[this.mascot], m = V.model, out = {};
    m.root.updateMatrixWorld(true);
    const p = new THREE.Vector3();
    for (const n of [1, 2]) {
      const item = this.items[n];
      if (!item || item.spec.mount === "head" || item.spec.mount === "wear") continue;
      let head = 0, body = 0, count = 0, at = null;
      item.root.traverse((x) => {
        // light, steam and sound rings are not solid: they may pass the mascot
        if (!x.isMesh || !x.geometry || !x.visible || x.userData.fx || x.material.transparent && x.material.opacity < 0.05) return;
        const pos = x.geometry.attributes.position;
        const step = Math.max(1, Math.floor(pos.count / 400));
        for (let i = 0; i < pos.count; i += step) {
          p.fromBufferAttribute(pos, i); x.localToWorld(p);
          const h = m.head.worldToLocal(p.clone());
          if (insideHead(h.x, h.y, h.z, -0.03)) { head = Math.max(head, 1); count++; at = at || ["head", +h.x.toFixed(2), +h.y.toFixed(2), +h.z.toFixed(2), x.name || x.geometry.type]; }
          if (V.kind === "body") {
            const b = m.bob.worldToLocal(p.clone());
            const r = bodyRadius(b.y);
            const d = r > 0 ? r - Math.hypot(b.x, b.z / 0.9) : -1;
            const belly = Math.hypot(b.x / 0.46, (b.y - 1.12) / 0.44, (b.z - 0.52) / 0.2);
            if (d > 0.03 || belly < 0.92) { body = Math.max(body, Math.max(d, (1 - belly) * 0.2)); count++; at = at || ["body", +b.x.toFixed(2), +b.y.toFixed(2), +b.z.toFixed(2), x.name || x.geometry.type]; }
          }
        }
      });
      out[item.id] = { head, body: Math.round(body * 1000) / 1000, count, at };
    }
    return out;
  }
  // where each mitten is, and how far from its item's grip (0 when the hand really holds it)
  gripReport() {
    const V = this.variants[this.mascot], P = V.pose, out = {};
    if (V.kind !== "body" || !P) return out;
    V.model.root.updateMatrixWorld(true);
    for (const i of [0, 1]) {
      const n = i ? P.G1 : P.G0, g = i ? P.g1 : P.g0;
      if (!n || !(g > 0.5) || !this.items[n] || !this.items[n].spec.grips) continue;
      const item = this.items[n];
      const mitten = V.model.arms[i].children[1].getWorldPosition(new THREE.Vector3());
      const g0 = item.spec.grips[i] || item.spec.grips[1 - i];
      const world = item.root.localToWorld(new THREE.Vector3(g0[0], g0[1], g0[2]));
      out[`hand${i}`] = { item: item.id, gap: Math.round(mitten.distanceTo(world) / V.size * 1000) / 1000 };
    }
    return out;
  }
}

// A paint stroke left in the air behind the brush tip: a ribbon that faces the camera and fades.
class Trail {
  constructor(scene) {
    this.n = 36;
    this.pts = [];
    const geo = new THREE.BufferGeometry();
    this.pos = new Float32Array(this.n * 2 * 3); this.col = new Float32Array(this.n * 2 * 4);
    geo.setAttribute("position", new THREE.BufferAttribute(this.pos, 3).setUsage(THREE.DynamicDrawUsage));
    geo.setAttribute("color", new THREE.BufferAttribute(this.col, 4).setUsage(THREE.DynamicDrawUsage));
    const idx = []; for (let i = 0; i < this.n - 1; i++) { const a = i * 2; idx.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
    geo.setIndex(idx);
    this.mesh = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, transparent: true, depthWrite: false, side: THREE.DoubleSide, toneMapped: false }));
    this.mesh.frustumCulled = false; this.mesh.visible = false;
    scene.add(this.mesh);
    this.v = new THREE.Vector3(); this.a = new THREE.Vector3(); this.b = new THREE.Vector3();
  }
  update(items, still, camera) {
    const brush = [items[1], items[2]].find((x) => x && x.spec.trail);
    if (!brush || still) { this.mesh.visible = false; this.pts.length = 0; return; }
    const tip = brush.root.localToWorld(this.v.set(0, 0.27, 0)).clone();
    this.pts.unshift(tip); if (this.pts.length > this.n) this.pts.length = this.n;
    const n = this.pts.length;
    if (n < 3) return;
    this.mesh.visible = true;
    const col = new THREE.Color(0xf6a08f);
    for (let i = 0; i < this.n; i++) {
      const p = this.pts[Math.min(i, n - 1)], q = this.pts[Math.min(i + 1, n - 1)], o = this.pts[Math.min(Math.max(i - 1, 0), n - 1)];
      this.a.subVectors(o, q).normalize();
      this.b.subVectors(camera.position, p).normalize();
      const side = this.a.cross(this.b).normalize();
      const k = i / (this.n - 1), w = 0.035 * (1 - k) * (i < n ? 1 : 0);
      this.pos.set([p.x + side.x * w, p.y + side.y * w, p.z + side.z * w, p.x - side.x * w, p.y - side.y * w, p.z - side.z * w], i * 6);
      const al = (1 - k) * 0.85 * (i < n ? 1 : 0);
      this.col.set([col.r, col.g, col.b, al, col.r, col.g, col.b, al], i * 8);
    }
    this.mesh.geometry.attributes.position.needsUpdate = true;
    this.mesh.geometry.attributes.color.needsUpdate = true;
  }
}
