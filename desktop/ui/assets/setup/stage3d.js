// The live 3D stage every demo stands on: renderer, light, the guide (Daedalus from the promo film,
// full body), its moods and moves, the camera rig, and the speech bubble. A demo builds its own set
// and choreography on top; nothing here knows which demo it is in.
import * as THREE from "./vendor/three.module.min.js";
import { makeDaedalus, poseDaedalus, makeChibi, poseChibi, COL } from "./chibi2.js";

export { THREE, COL, makeChibi, poseChibi };

export const reducedMotion = () =>
  document.documentElement.classList.contains("reduce") || matchMedia("(prefers-reduced-motion: reduce)").matches;
if (new URLSearchParams(location.search).get("motion") === "0") document.documentElement.classList.add("reduce");

const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
export const ease = {
  inOut: (k) => (k < 0.5 ? 4 * k * k * k : 1 - Math.pow(-2 * k + 2, 3) / 2),
  out: (k) => 1 - Math.pow(1 - k, 3),
  in: (k) => k * k * k,
  // overshoots a touch and settles: for things that land
  back: (k) => 1 + 2.2 * Math.pow(k - 1, 3) + 1.2 * Math.pow(k - 1, 2),
};

// One clock for the whole page. Tweens are plain records advanced by the render loop, so a paused
// page (hidden tab) also pauses every move instead of jumping when it comes back.
const tweens = new Set();
export function tween(dur, fn, curve) {
  return new Promise((resolve) => {
    if (reducedMotion() || dur <= 0) { fn(1, 1); resolve(); return; }
    tweens.add({ t: 0, dur, fn, curve: curve || ease.inOut, resolve });
  });
}
const wait = (s) => tween(s, () => {});
export { wait };
let holds = 0;
// Something the readiness flag has to wait for that is not a tween (a camera still gliding).
export function hold(p) { holds++; return Promise.resolve(p).finally(() => { holds--; }); }

export function createStage(o) {
  o = o || {};
  const canvas = o.canvas;
  const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "default", alpha: !!o.transparent, premultipliedAlpha: true });
  // A laptop GPU at 2x on a 1440 window draws four times the pixels for little visible gain
  // on soft, rounded shapes; 1.5 is the compromise the film's still frames were judged at.
  const dpr = () => Math.min(window.devicePixelRatio || 1, o.maxDpr || 1.5);
  renderer.setPixelRatio(dpr());
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.NeutralToneMapping;
  renderer.toneMappingExposure = o.exposure || 1.05;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFShadowMap; // the soft variant is deprecated and three falls back to this one anyway
  const scene = new THREE.Scene();
  // transparent: the canvas sits over a page that draws its own background (a strip, a card)
  if (o.transparent) renderer.setClearColor(0x000000, 0);
  else scene.background = new THREE.Color(o.background == null ? 0x0b0d10 : o.background);
  if (o.fog) scene.fog = new THREE.Fog(o.background == null ? 0x0b0d10 : o.background, o.fog[0], o.fog[1]);
  const camera = new THREE.PerspectiveCamera(o.fov || 32, 1, 0.05, 80);
  // The film's studio: a grey box with three soft panels (white overhead, mint behind, warm left),
  // pre-filtered once, so the glossy head and bronze body have something to reflect.
  {
    const env = new THREE.Scene();
    env.background = new THREE.Color(0x3a3e44);
    env.add(new THREE.Mesh(new THREE.BoxGeometry(20, 10, 20), new THREE.MeshBasicMaterial({ color: 0x2c3036, side: THREE.BackSide })));
    const lamp = (x, y, z, w, h, c, ry, rx) => { const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ color: c, side: THREE.DoubleSide })); m.position.set(x, y, z); m.rotation.set(rx || 0, ry || 0, 0); env.add(m); };
    lamp(0, 4.9, 0, 8, 3, 0xffffff, 0, Math.PI / 2);
    lamp(0, 1.5, -9.9, 8, 3, 0xcff5ea, 0);
    lamp(-9.9, 2, 0, 5, 3, 0xffe6cc, Math.PI / 2);
    lamp(9.9, 1.2, 2, 4, 2, 0xdde6e4, -Math.PI / 2);
    lamp(0, -4.9, 0, 20, 20, 0x15171a, 0, -Math.PI / 2);
    const pm = new THREE.PMREMGenerator(renderer);
    scene.environment = pm.fromScene(env, 0.04).texture;
    pm.dispose();
    scene.environmentIntensity = o.envIntensity == null ? 0.6 : o.envIntensity;
  }

  // Camera rig: where it is and what it looks at glide toward their targets every frame, so a
  // demo only says where the camera should be and the move comes out smooth and interruptible.
  const rig = {
    pos: new THREE.Vector3(0, 1, 4), look: new THREE.Vector3(0, 0.5, 0),
    toPos: new THREE.Vector3(0, 1, 4), toLook: new THREE.Vector3(0, 0.5, 0),
    stiffness: 3.2,
    set(pos, look, snap) {
      this.toPos.copy(pos); this.toLook.copy(look);
      if (snap || reducedMotion()) { this.pos.copy(pos); this.look.copy(look); }
    },
    moving() { return this.pos.distanceTo(this.toPos) > 0.004 || this.look.distanceTo(this.toLook) > 0.004; },
  };

  // The zone of the window the 3D subject should sit in (the part the wizard frame does not
  // cover). The projection centre is shifted there, so the camera can look straight at the
  // guide and the guide still lands beside the frame rather than behind it.
  let zone = null, W = 1, H = 1;
  function measure() {
    // a canvas that is not full-window (a strip, a slot) sizes to its own box
    if (o.fitCanvas) { const r = canvas.getBoundingClientRect(); W = Math.max(1, r.width); H = Math.max(1, r.height); }
    else { W = window.innerWidth; H = window.innerHeight; }
    renderer.setPixelRatio(dpr());
    renderer.setSize(W, H, false);
    camera.aspect = W / H;
    const z = o.zone && o.zone();
    zone = z ? { cx: z.left + z.width / 2, cy: z.top + z.height / 2, w: z.width, h: z.height } : { cx: W / 2, cy: H / 2, w: W, h: H };
    camera.setViewOffset(W, H, -(zone.cx - W / 2), -(zone.cy - H / 2), W, H);
    camera.updateProjectionMatrix();
    if (o.onResize) o.onResize({ W, H, zone });
  }
  // Distance at which an object `height` tall fills `fill` of the zone's height (and never more
  // than `fill` of its width for an object `width` wide).
  function distanceFor(height, width, fill) {
    const vh = height / (fill * (zone.h / H));
    const vw = (width || height) / (fill * (zone.w / W)) / camera.aspect;
    return Math.max(vh, vw) / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)));
  }

  let last = performance.now(), running = true, raf = 0, frames = 0, clock = 0;
  const onFrame = [];
  function loop(now) {
    raf = 0;
    if (!running) return;
    if (o.maxFps && now - last < 1000 / o.maxFps) { schedule(); return; }
    // The readiness flag the screenshot harness waits on: no tween, no camera glide, the wizard
    // not between steps. It is read here, at the start of a frame, because a tween that finishes
    // often starts the next one from a promise callback, which only runs after the frame that
    // finished it; read at the end of that frame, the flag went up for one frame between the two.
    {
      const wz = window.Wizard;
      const settled = frames > 0 && tweens.size === 0 && holds === 0 && !rig.moving() && !(wz && wz.busy && wz.busy());
      const flag = settled ? "1" : "0";
      if (document.body.dataset.settled !== flag) document.body.dataset.settled = flag;
    }
    // capped so a stalled tab does not jump, but loose enough that a slow GPU keeps real time
    const dt = Math.min(0.1, (now - last) / 1000);
    last = now;
    clock += dt;
    for (const tw of Array.from(tweens)) {
      tw.t += dt;
      const k = clamp(tw.t / tw.dur, 0, 1);
      tw.fn(tw.curve(k), k);
      if (k >= 1) { tweens.delete(tw); tw.resolve(); }
    }
    const a = 1 - Math.exp(-rig.stiffness * dt);
    rig.pos.lerp(rig.toPos, a); rig.look.lerp(rig.toLook, a);
    camera.position.copy(rig.pos); camera.lookAt(rig.look);
    for (const f of onFrame) f(dt, clock);
    renderer.render(scene, camera);
    frames++;
    schedule();
  }
  function schedule() { if (!raf && running) raf = requestAnimationFrame(loop); }
  // WebGL stops when the page is hidden: nothing is drawn for nobody, and the laptop stays cool.
  const visibility = () => {
    running = !document.hidden;
    if (running) { last = performance.now(); schedule(); }
    else if (raf) { cancelAnimationFrame(raf); raf = 0; }
  };
  document.addEventListener("visibilitychange", visibility);
  window.addEventListener("resize", measure);

  return {
    THREE, renderer, scene, camera, rig,
    measure, distanceFor,
    get zone() { return zone; }, get size() { return { W, H }; },
    onFrame: (f) => onFrame.push(f),
    start() { measure(); last = performance.now(); schedule(); },
    dispose() {
      running = false;
      if (raf) { cancelAnimationFrame(raf); raf = 0; }
      document.removeEventListener("visibilitychange", visibility);
      window.removeEventListener("resize", measure);
      scene.traverse((node) => {
        if (node.geometry) node.geometry.dispose();
        if (node.material) for (const material of (Array.isArray(node.material) ? node.material : [node.material])) material.dispose();
      });
      scene.environment?.dispose();
      renderer.dispose();
      renderer.forceContextLoss();
    },
    frames: () => frames,
    time: () => clock,
  };
}

// A warm key light with soft shadows, a cool mint rim from behind, and a faint fill: the light
// the film's mascot shots use. `target` is the centre of the area that throws shadows.
export function studioLights(scene, o) {
  o = o || {};
  const hemi = new THREE.HemisphereLight(0xfff1e2, 0x101214, o.hemi == null ? 0.35 : o.hemi);
  scene.add(hemi);
  const key = new THREE.DirectionalLight(0xffe3c4, o.key == null ? 2.2 : o.key);
  key.position.set(-2.2, 3.6, 3.2);
  key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  const sc = key.shadow.camera; const r = o.shadowRadius || 1.6;
  sc.left = -r; sc.right = r; sc.top = r; sc.bottom = -r; sc.near = 0.5; sc.far = 12;
  key.shadow.bias = -0.0004; key.shadow.normalBias = 0.02; key.shadow.radius = 4;
  scene.add(key, key.target);
  const rim = new THREE.SpotLight(0x8ff0d6, o.rim == null ? 9 : o.rim, 10, 0.6, 0.8, 1.4);
  rim.position.set(1.8, 2.6, -2.6);
  scene.add(rim, rim.target);
  const fill = new THREE.PointLight(0xbfd8ff, o.fill == null ? 0.8 : o.fill, 8, 1.6);
  fill.position.set(2.5, 1.2, 2.5);
  scene.add(fill);
  const aim = (p) => {
    key.target.position.copy(p); key.position.set(p.x - 2.2, p.y + 3.6, p.z + 3.2);
    rim.target.position.copy(p); rim.position.set(p.x + 1.8, p.y + 2.6, p.z - 2.6);
    fill.position.set(p.x + 2.5, p.y + 1.2, p.z + 2.5);
  };
  aim(o.target || new THREE.Vector3());
  return { hemi, key, rim, fill, aim };
}

// A soft round contact shadow (the film's blob), for things that hover or stand on a surface the
// shadow map does not reach cleanly.
const blobTex = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 128;
  const g = c.getContext("2d"); const r = g.createRadialGradient(64, 64, 0, 64, 64, 64);
  r.addColorStop(0, "rgba(0,0,0,.85)"); r.addColorStop(0.5, "rgba(0,0,0,.35)"); r.addColorStop(1, "rgba(0,0,0,0)");
  g.fillStyle = r; g.fillRect(0, 0, 128, 128);
  return new THREE.CanvasTexture(c);
})();
export function contactShadow(size) {
  const m = new THREE.Mesh(new THREE.PlaneGeometry(size, size), new THREE.MeshBasicMaterial({ map: blobTex, transparent: true, depthWrite: false, opacity: 0.7 }));
  m.rotation.x = -Math.PI / 2; m.renderOrder = 1;
  return m;
}

// ---- the guide ----
// Daedalus walks, hops and flies between marks, turns to whoever it talks to, points at what to
// fill, and has moods that fade in and out on their own. Every move returns a promise.
export class Guide {
  constructor(stage, o) {
    o = o || {};
    this.stage = stage;
    this.size = o.size || 0.1;
    this.d = makeDaedalus(this.size);
    this.root = this.d.root;
    stage.scene.add(this.root);
    this.height = 4.25 * this.size;
    this.pos = new THREE.Vector3();
    this.yaw = 0; this.toYaw = 0;
    this.lift = 0;
    this.run = 0; this.phase = 0; this.hop = 0; this.flap = 0; this.legsUp = 0;
    this.mood = { happy: 0, worry: 0, nod: 0, wave: 0, shrug: 0, glow: 1 };
    this.moodTo = { happy: 0, worry: 0, wave: 0, shrug: 0, glow: 1 };
    this.point = null; this.pointK = 0;
    this.gaze = { x: 0, y: 0 };
    this.lookAtCamera = true;
    this.timers = [];
    this.moveId = 0;
    this.jumpId = 0;
    this.squash = 1;
    this.shadow = o.shadow === false ? null : contactShadow(this.size * 9);
    if (this.shadow) stage.scene.add(this.shadow);
    this.ground = 0;
    this.d.root.traverse((m) => { if (m.isMesh) { m.castShadow = true; } });
    stage.onFrame((dt, t) => this.update(dt, t));
  }
  // Every move takes a number; a move that a newer one replaced stops writing, so a quick
  // double click on Next never has two walks pulling the guide in two directions.
  place(p) { this.moveId++; this.run = 0; this.flap = 0; this.legsUp = 0; this.pos.copy(p); this.ground = p.y; return this; }
  anchor(out) {
    // the point the speech bubble hangs from: just above the crest
    return (out || new THREE.Vector3()).set(this.pos.x, this.pos.y + this.lift + this.height * 1.02, this.pos.z);
  }
  hand(i, out) {
    const m = this.d.arms[i == null ? 1 : i].children[1];
    return m.getWorldPosition(out || new THREE.Vector3());
  }
  faceToward(p) { this.toYaw = Math.atan2(p.x - this.pos.x, p.z - this.pos.z); this.lookAtCamera = false; }
  faceCamera() { this.lookAtCamera = true; }

  // A mood is a pulse: it rises, holds and fades on its own.
  feel(name, hold, amount) {
    const v = amount == null ? 1 : amount;
    this.moodTo[name] = v;
    const id = setTimeout(() => { this.moodTo[name] = 0; }, (reducedMotion() ? 0.6 : hold || 1.4) * 1000);
    this.timers.push(id);
  }
  calm() { this.timers.forEach(clearTimeout); this.timers = []; for (const k in this.moodTo) if (k !== "glow") this.moodTo[k] = 0; this.point = null; }
  happy(hold) { this.moodTo.worry = 0; this.feel("happy", hold || 1.6); if (!reducedMotion()) this.jump(0.5, 0.07); }
  worried(hold) { this.moodTo.happy = 0; this.feel("worry", hold || 2.2); this.feel("shrug", 0.9, 0.8); }
  nod() {
    if (reducedMotion()) return Promise.resolve();
    return tween(0.55, (k, raw) => { this.mood.nod = Math.sin(raw * Math.PI * 2) * 0.5 * (1 - raw) + Math.sin(raw * Math.PI) * 0.6; }, (k) => k).then(() => { this.mood.nod = 0; });
  }
  wave(hold) { this.feel("wave", hold || 1.8); }
  // Points with the arm on the side of `side` (-1 viewer's left, +1 viewer's right); up lifts it.
  pointAt(side, up) { this.point = { side: side || 1, up: up || 0 }; }
  stopPointing() { this.point = null; }

  // A jump: crouch, stretch on take-off, feet tucked in the air, squash on landing. The whole
  // figure leaves the ground: the lift goes on the root, not on the upper body, because the legs
  // hang from the root and a lifted torso left the boots planted on the floor.
  jump(dur, h) {
    if (reducedMotion()) return Promise.resolve();
    const id = ++this.jumpId;
    const total = dur + 0.16;
    const c0 = 0.08 / total, c1 = 1 - 0.08 / total; // crouch before, settle after
    const h0 = this.hop; // a jump that cuts into another starts from where that one was
    return tween(total, (k, raw) => {
      if (id !== this.jumpId) return;
      if (raw < c0) { const q = raw / c0; this.hop = h0 * (1 - q); this.squash = 1 - 0.12 * Math.sin(q * Math.PI / 2); this.legsUp = 0; return; }
      if (raw > c1) { const q = (raw - c1) / (1 - c1); this.hop = 0; this.squash = 1 - 0.13 * Math.sin((1 - q) * Math.PI / 2) * (1 - q * 0.2); this.legsUp = 0; return; }
      const q = (raw - c0) / (c1 - c0);
      this.hop = Math.sin(q * Math.PI) * h;
      // stretched while rising fast, round at the top, stretched again just before touching down
      this.squash = 1 + 0.1 * Math.abs(Math.cos(q * Math.PI)) * (1 - Math.sin(q * Math.PI) * 0.6);
      this.legsUp = -0.45 * Math.sin(q * Math.PI);
    }, (k) => k).then(() => { if (id !== this.jumpId) return; this.hop = 0; this.legsUp = 0; this.squash = 1; });
  }
  // Walk to a mark on the same level: the steps follow the distance, the body turns into the
  // walk and back to the viewer at the end.
  walkTo(p, speed) {
    const from = this.pos.clone(), to = p.clone();
    const dist = from.distanceTo(to);
    if (dist < 1e-3) return Promise.resolve();
    const dur = reducedMotion() ? 0 : clamp(dist / (speed || this.size * 5.5), 0.5, 2.6);
    const wasCam = this.lookAtCamera;
    this.faceToward(to);
    const id = ++this.moveId;
    return tween(dur, (k, raw) => {
      if (id !== this.moveId) return;
      this.pos.lerpVectors(from, to, k);
      this.run = Math.min(1, Math.sin(raw * Math.PI) * 2.2);
      this.phase = (k * dist) / (this.size * 0.55);
    }, ease.inOut).then(() => { if (id !== this.moveId) return; this.run = 0; this.ground = to.y; if (wasCam) this.faceCamera(); });
  }
  // A hop: an arc, wings fluttering at the top. For short distances and steps up or down.
  hopTo(p, h) {
    const from = this.pos.clone(), to = p.clone();
    const height = h == null ? this.height * 0.5 + Math.max(0, to.y - from.y) : h;
    const wasCam = this.lookAtCamera;
    if (from.distanceTo(to) > 1e-3) this.faceToward(to);
    const id = ++this.moveId;
    return tween(reducedMotion() ? 0 : 0.75, (k, raw) => {
      if (id !== this.moveId) return;
      this.pos.lerpVectors(from, to, k);
      this.pos.y += Math.sin(raw * Math.PI) * height;
      this.flap = Math.sin(raw * Math.PI);
      this.legsUp = -Math.sin(raw * Math.PI) * 0.6;
    }, ease.inOut).then(() => { if (id !== this.moveId) return; this.flap = 0; this.legsUp = 0; this.ground = to.y; if (wasCam) this.faceCamera(); return this.jump(0.22, this.size * 0.15); });
  }
  // Flight: take off, travel high, land. For long moves and the exit.
  flyTo(p, o) {
    o = o || {};
    const from = this.pos.clone(), to = p.clone();
    const high = o.high == null ? this.height * 1.2 : o.high;
    const wasCam = this.lookAtCamera;
    this.faceToward(to);
    const dur = reducedMotion() ? 0 : o.dur || clamp(from.distanceTo(to) / (this.size * 9) + 0.9, 1, 2.8);
    const id = ++this.moveId;
    return tween(dur, (k, raw) => {
      if (id !== this.moveId) return;
      this.pos.lerpVectors(from, to, k);
      const arc = Math.sin(raw * Math.PI);
      this.pos.y += arc * high;
      this.flap = Math.min(1, arc * 3);
      this.legsUp = -0.5 * Math.min(1, arc * 3);
      this.run = 0;
    }, o.curve || ease.inOut).then(() => { if (id !== this.moveId) return; this.flap = 0; this.legsUp = 0; this.ground = to.y; if (wasCam && !o.keepFacing) this.faceCamera(); });
  }

  update(dt, t) {
    const a = 1 - Math.exp(-6 * dt);
    for (const k in this.moodTo) this.mood[k] += (this.moodTo[k] - this.mood[k]) * a;
    this.pointK += ((this.point ? 1 : 0) - this.pointK) * (1 - Math.exp(-7 * dt));
    if (this.lookAtCamera) { const c = this.stage.camera.position; this.toYaw = Math.atan2(c.x - this.pos.x, c.z - this.pos.z) * 0.85; }
    let dy = this.toYaw - this.yaw; dy = Math.atan2(Math.sin(dy), Math.cos(dy));
    this.yaw += dy * (1 - Math.exp(-8 * dt));
    const still = reducedMotion();
    this.root.position.set(this.pos.x, this.pos.y + this.lift + this.hop, this.pos.z);
    // squash and stretch about the feet (the scaled group's origin is at the soles), volume kept
    const sq = this.squash;
    this.d.s.scale.set(this.size / Math.sqrt(sq), this.size * sq, this.size / Math.sqrt(sq));
    this.root.rotation.y = this.yaw;
    const arms = [null, null];
    if (this.pointK > 0.01 && this.point) {
      // the arm on the pointing side reaches out and slightly toward the viewer
      const i = this.point.side > 0 ? 1 : 0;
      arms[i] = { x: -0.35, z: 1.35 + 0.6 * this.point.up, y: 0, k: this.pointK };
    }
    const m = this.mood;
    poseDaedalus(this.d, {
      t: still ? 0.4 : t, seed: 0.7, run: this.run, phase: this.phase, hop: 0,
      flap: Math.max(this.flap, m.happy * 0.25), legsUp: this.legsUp,
      happy: m.happy, wave: m.wave, shrug: m.shrug, worry: m.worry, nod: m.nod, glow: m.glow,
      arms, gazeX: this.gaze.x, gazeY: this.gaze.y, headYaw: this.lookAtCamera ? 0 : null,
    });
    if (this.shadow) {
      const air = this.pos.y + this.lift + this.hop - this.ground;
      this.shadow.position.set(this.pos.x, this.ground + 0.002, this.pos.z);
      const s = 1 / (1 + air * 3 / this.height);
      this.shadow.scale.setScalar(s);
      this.shadow.material.opacity = 0.65 * s;
    }
  }
}

// ---- the speech bubble ----
// A DOM bubble hung from a world point; it follows every frame and stays on screen.
export function createBubble(stage, el, getAnchor) {
  const v = new THREE.Vector3();
  let text = "", shown = false, typeTimer = 0;
  const chrome = Array.from(document.querySelectorAll(".brand, .lang-box"));
  stage.onFrame(() => {
    if (!shown) return;
    getAnchor(v); v.project(stage.camera);
    const { W, H } = stage.size;
    let x = (v.x * 0.5 + 0.5) * W, y = (-v.y * 0.5 + 0.5) * H;
    const r = el.getBoundingClientRect();
    const half = r.width / 2;
    let cx = clamp(x, half + 10, W - half - 10);
    let top = clamp(y - r.height - 10, 8, H - r.height - 8);
    // Above the head is the page's corner chrome on a phone (brand, language switch). When the
    // bubble would sit on it, it moves beside the head instead, its tail pointing left.
    const hit = (q) => q && q.width && cx - half < q.right + 6 && cx + half > q.left - 6 && top < q.bottom + 6 && top + r.height > q.top - 6;
    let side = false;
    if (chrome.some((c) => hit(c.getBoundingClientRect()))) {
      side = true;
      cx = clamp(x + 56 + half, half + 10, W - half - 10);
      top = clamp(y + 6, 8, H - r.height - 8);
    }
    el.classList.toggle("side", side);
    el.style.transform = `translate3d(${Math.round(cx - half)}px, ${Math.round(top)}px, 0)`;
    el.style.setProperty("--tail", `${clamp(x - (cx - half), 18, r.width - 18)}px`);
  });
  return {
    say(s) {
      if (s === text && shown) return;
      text = s; shown = !!s;
      clearInterval(typeTimer);
      el.classList.remove("on");
      void el.offsetWidth;
      if (!s) { el.hidden = true; return; }
      el.hidden = false;
      const span = el.querySelector("span") || el;
      if (reducedMotion()) span.textContent = s;
      else {
        // typed in quickly, a word at a time: reads as speech without making anyone wait
        const words = s.split(" "); let n = 0; span.textContent = "";
        typeTimer = setInterval(() => { n++; span.textContent = words.slice(0, n).join(" "); if (n >= words.length) clearInterval(typeTimer); }, 55);
      }
      el.classList.add("on");
    },
    hide() { this.say(""); },
  };
}

// The guide's own lines — short on purpose, one breath each — are in the launcher's table with the wizard's (desktop/i18n.go, hero.say.*),
// so the wizard's t() finds them; nothing here holds a sentence.

// Text that the guide reacts to: one place that maps a wizard change to a line and a mood, so
// every demo reacts the same way and only stages it differently.
export function reactionFor(path, S) {
  if (path === "mode") return { say: S.mode === "docker" ? "say.runs.docker" : "say.runs.native", mood: "nod" };
  if (path === "kind") return { say: "say.model." + (S.kind === "cloud" ? "key" : S.kind), mood: "nod" };
  if (path === "provider" || path === "cli") return { mood: "nod" };
  if (path === "local.test") {
    if (S.local.test === "busy") return { say: "say.test.busy", mood: "think" };
    if (S.local.test === "ok") return { say: "say.test.ok", mood: "happy" };
    if (S.local.test === "fail") return { say: "say.test.fail", mood: "worry" };
  }
  if (path === "limit") {
    const v = Number(S.limit);
    if (v && v <= 5) return { say: "say.limit.low", mood: "nod" };
    if (v >= 100) return { say: "say.limit.high", mood: "happy" };
    return { mood: "nod" };
  }
  if (path === "adv.open") return { mood: "nod" };
  if (path && path.indexOf("keys.") === 0) {
    const v = S.keys[S.provider] || "";
    if (v.length === 12) return { say: "say.model.key", mood: "nod" };
  }
  return null;
}

// Hooks the wizard calls, routed to the stage's handlers.
export function wireWizard(h) {
  window.WizardHooks = Object.assign({ handoffDelay: 1700 }, h);
}
