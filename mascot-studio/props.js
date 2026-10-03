// The props: small hand-made objects in the mascot's own units (its head is two units wide, a
// mitten 0.34 across), each with the facts the motion needs to use it rather than show it beside
// the mascot: how it is mounted (in one hand, in both, on the plinth, floating, worn), where the
// hands grip it, how it is turned for reading, offering or carrying, the work it is for, and
// the action it suggests. Built from rounded shapes in the studio's warm palette; text and
// pictures on paper and screens are drawn on canvases, so a calendar shows a date and a clock
// the time.
import { THREE } from "../desktop/ui/assets/setup/stage3d.js";
import { RoundedBoxGeometry } from "../desktop/ui/assets/setup/vendor/RoundedBoxGeometry.js";
import { sipPhase } from "./motion.js";

const TAU = Math.PI * 2;
export const C = {
  paper: 0xf6ead3, cream: 0xfff6e6, ink: 0x34434d, coral: 0xf08a7e, rose: 0xf2777f, mint: 0x6fe3c4,
  teal: 0x2f6467, deep: 0x22363b, gold: 0xe3b25c, wood: 0xb47a4c, woodDark: 0x7a5034, charcoal: 0x2b3136,
  steel: 0xa9bac4, sky: 0x8fc4ee, lilac: 0xab9cec, leaf: 0x67c27e, leafDark: 0x3f9a5c, terracotta: 0xd5805a,
  sand: 0xecc77f, white: 0xffffff,
};

const shared = new Map();
// materials are shared between builds of the same look; animated ones are made per build
function mat(color, o = {}) {
  const key = color + "|" + JSON.stringify(o);
  if (!shared.has(key)) shared.set(key, new THREE.MeshPhysicalMaterial({ color, roughness: 0.55, metalness: 0, clearcoat: 0.25, clearcoatRoughness: 0.45, ...o }));
  return shared.get(key);
}
const metal = (color) => mat(color, { metalness: 0.65, roughness: 0.32, clearcoat: 0.5 });
const glossy = (color) => mat(color, { roughness: 0.3, clearcoat: 0.9, clearcoatRoughness: 0.15 });
const glow = (color, k = 1) => new THREE.MeshBasicMaterial({ color: new THREE.Color(color).multiplyScalar(k), toneMapped: false });
const glass = (color = 0xd9f3ff, opacity = 0.28) => mat(color, { roughness: 0.05, clearcoat: 1, transparent: true, opacity, depthWrite: false, side: THREE.DoubleSide });

function mesh(geo, material, pos, rot, scl) {
  const m = new THREE.Mesh(geo, material);
  if (pos) m.position.set(pos[0], pos[1], pos[2]);
  if (rot) m.rotation.set(rot[0], rot[1], rot[2]);
  if (scl) { if (typeof scl === "number") m.scale.setScalar(scl); else m.scale.set(scl[0], scl[1], scl[2]); }
  m.castShadow = true;
  return m;
}
const group = (...parts) => { const g = new THREE.Group(); parts.forEach((p) => p && g.add(p)); return g; };
const rbox = (w, h, d, r, material, pos, rot) => mesh(new RoundedBoxGeometry(w, h, d, 3, Math.min(r, w / 2, h / 2, d / 2) - 1e-4), material, pos, rot);
const cyl = (rt, rb, h, material, pos, rot, seg = 28) => mesh(new THREE.CylinderGeometry(rt, rb, h, seg), material, pos, rot);
const ball = (r, material, pos, scl, seg = 24) => mesh(new THREE.SphereGeometry(r, seg, Math.round(seg * 0.7)), material, pos, null, scl);
const torus = (r, t, material, pos, rot, arc = TAU) => mesh(new THREE.TorusGeometry(r, t, 12, 40, arc), material, pos, rot);
const lathe = (pts, material, pos, seg = 36) => mesh(new THREE.LatheGeometry(pts.map(([x, y]) => new THREE.Vector2(x, y)), seg), material, pos);
const tube = (pts, r, material, seg = 32) => mesh(new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts.map(([x, y, z]) => new THREE.Vector3(x, y, z))), seg, r, 8, false), material);
function extrude(shape, depth, material, bevel = 0.02, pos, rot) {
  const geo = new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: bevel > 0, bevelSize: bevel, bevelThickness: bevel, bevelSegments: 3, curveSegments: 18 });
  geo.translate(0, 0, -depth / 2);
  return mesh(geo, material, pos, rot);
}
function plane(w, h, material, pos, rot) { const m = mesh(new THREE.PlaneGeometry(w, h), material, pos, rot); m.castShadow = false; return m; }

// Drawings on paper and screens. Cached by key: the same picture is drawn once.
const textures = new Map();
function canvasTexture(key, w, h, draw) {
  if (textures.has(key)) return textures.get(key);
  const c = document.createElement("canvas"); c.width = w; c.height = h;
  draw(c.getContext("2d"), w, h);
  const t = new THREE.CanvasTexture(c);
  t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
  textures.set(key, t);
  return t;
}
const paperMat = (key, w, h, draw) => mat(C.paper, { map: canvasTexture(key, w, h, draw), roughness: 0.85, clearcoat: 0 });
const screenMat = (key, w, h, draw) => new THREE.MeshBasicMaterial({ map: canvasTexture(key, w, h, draw), toneMapped: false });
const css = (hex) => "#" + hex.toString(16).padStart(6, "0");
function lines(g, x0, x1, ys, color, width = 6) { g.strokeStyle = color; g.lineWidth = width; g.lineCap = "round"; ys.forEach((y) => { g.beginPath(); g.moveTo(x0, y); g.lineTo(x1, y); g.stroke(); }); }
function scribble(g, x0, x1, y, seed, color, width = 5) {
  g.strokeStyle = color; g.lineWidth = width; g.lineCap = "round"; g.lineJoin = "round"; g.beginPath(); g.moveTo(x0, y);
  for (let x = x0; x <= x1; x += 6) g.lineTo(x, y + Math.sin(x * 0.21 + seed) * 6 + Math.sin(x * 0.07 + seed * 3) * 3);
  g.stroke();
}
function star(r1, r2, n = 5) {
  const s = new THREE.Shape();
  for (let i = 0; i < n * 2; i++) { const a = Math.PI / 2 + (i * Math.PI) / n, r = i % 2 ? r2 : r1; const x = Math.cos(a) * r, y = Math.sin(a) * r; if (!i) s.moveTo(x, y); else s.lineTo(x, y); }
  s.closePath();
  return s;
}
function heartShape(r) {
  const s = new THREE.Shape(); s.moveTo(0, -r);
  s.bezierCurveTo(-r * 2.15, r * 0.15, -r * 0.95, r * 1.55, 0, r * 0.6);
  s.bezierCurveTo(r * 0.95, r * 1.55, r * 2.15, r * 0.15, 0, -r);
  return s;
}
function roundRect(w, h, r) {
  const s = new THREE.Shape(), x = -w / 2, y = -h / 2;
  s.moveTo(x + r, y); s.lineTo(x + w - r, y); s.quadraticCurveTo(x + w, y, x + w, y + r); s.lineTo(x + w, y + h - r);
  s.quadraticCurveTo(x + w, y + h, x + w - r, y + h); s.lineTo(x + r, y + h); s.quadraticCurveTo(x, y + h, x, y + h - r);
  s.lineTo(x, y + r); s.quadraticCurveTo(x, y, x + r, y);
  return s;
}
// the brand mark (the ✳ in the header): three crossed bars
function brandMark(r, material) {
  const g = new THREE.Group();
  for (let i = 0; i < 4; i++) { const b = mesh(new THREE.CapsuleGeometry(r * 0.16, r * 1.7, 4, 10), material, null, [0, 0, (i * Math.PI) / 4]); b.castShadow = false; g.add(b); }
  return g;
}
// soft puffs of steam that rise and fade; returns a ticker
function steam(parent, origin, n = 3, size = 0.07, height = 0.5) {
  const puffs = Array.from({ length: n }, () => {
    const m = ball(size, new THREE.MeshBasicMaterial({ color: 0xffffff, transparent: true, opacity: 0, depthWrite: false }), origin, null, 12);
    m.castShadow = false; m.userData.fx = true; parent.add(m); return m;
  });
  return (t, still, hide) => puffs.forEach((m, i) => {
    if (hide) { m.material.opacity = Math.max(0, m.material.opacity - 0.08); return; }
    const ph = still ? (i + 0.5) / n : (t / 2.4 + i / n) % 1;
    m.position.set(origin[0] + Math.sin(ph * 5 + i * 2) * 0.05, origin[1] + ph * height, origin[2] + Math.cos(ph * 4 + i) * 0.03);
    m.scale.setScalar(0.6 + ph * 1.2);
    m.material.opacity = Math.sin(ph * Math.PI) * 0.32;
  });
}

const sipping = (e, still) => sipPhase(e, still).up > 0.15;

// ---- the library ----
// mount: "hand" (one hand; `hand` 0 for the arm on the left of the screen), "both", "ground" (on
// the plinth beside the mascot), "float" (hovers or flies by itself), "head" (worn on the head),
// "wear" (around the shoulders). grips: per hand, a point of the item in its own units. carry /
// show / read / offer / hug / raise / inspect / tinker / speak: how it is turned (XYZ Euler) in
// those holds; readY/readZ, hugY/hugZ, offerY/offerZ, sleepY/sleepZ: where a two-handed item
// sits; carryAt, tuck: a carry spot of its own. work: what the work actions do with it. use: the
// action that suits it when it is picked on its own. carryOnly: never lifted to be shown (a
// balloon on its string); follow: what a floating thing keeps beside (the head, the body);
// watch: the eyes follow it while idle; companion: a second item it comes with.
export const PROPS = {
  book: {
    mount: "both", work: "read", use: "read", grips: [[-0.43, -0.12, -0.02], [0.43, -0.12, -0.02]],
    read: [-0.72, 0, 0], readY: 1.48, readZ: 1.0, hug: [0.08, 0, 0], hugY: 1.28, hugZ: 1.0, offer: [0.18, 0, 0], offerY: 1.5, offerZ: 1.1,
    build() {
      const pageL = paperMat("book-l", 256, 320, (g) => { g.fillStyle = css(C.cream); g.fillRect(0, 0, 256, 320); for (let i = 0; i < 9; i++) scribble(g, 34, 222 - (i % 3) * 30, 50 + i * 28, i, "#8aa3b4", 5); });
      const pageR = paperMat("book-r", 256, 320, (g) => {
        g.fillStyle = css(C.cream); g.fillRect(0, 0, 256, 320);
        g.fillStyle = css(C.mint); g.beginPath(); g.arc(128, 92, 46, 0, TAU); g.fill();
        g.fillStyle = css(C.gold); g.beginPath(); g.arc(150, 76, 18, 0, TAU); g.fill();
        for (let i = 0; i < 5; i++) scribble(g, 34, 222 - (i % 2) * 40, 172 + i * 28, i + 4, "#8aa3b4", 5);
      });
      const halves = [-1, 1].map((sx) => {
        const h = new THREE.Group(); h.rotation.y = -sx * 0.16;
        h.add(rbox(0.43, 0.58, 0.03, 0.012, glossy(C.coral), [sx * 0.215, 0, 0]));
        h.add(rbox(0.41, 0.54, 0.035, 0.01, mat(C.cream, { roughness: 0.9, clearcoat: 0 }), [sx * 0.21, 0, 0.032]));
        h.add(plane(0.38, 0.5, sx < 0 ? pageL : pageR, [sx * 0.205, 0, 0.051]));
        return h;
      });
      const g = group(...halves, cyl(0.022, 0.022, 0.58, glossy(C.coral), [0, 0, -0.004]));
      g.add(rbox(0.04, 0.34, 0.008, 0.004, glossy(C.mint), [0.05, -0.36, 0.05]));
      // a page that turns over now and then
      const leaf = new THREE.Group(); leaf.position.set(0, 0, 0.056);
      const sheet = plane(0.38, 0.5, mat(C.cream, { roughness: 0.9, clearcoat: 0, side: THREE.DoubleSide }), [0.195, 0, 0]);
      leaf.add(sheet); leaf.visible = false; g.add(leaf);
      g.userData.leaf = leaf;
      return g;
    },
    tick(g, t, c) {
      const leaf = g.userData.leaf;
      if (c.still || c.action !== "read") { leaf.visible = false; return; }
      const ph = (c.e % 7.6) / 7.6, k = Math.min(1, Math.max(0, (ph - 0.82) / 0.14));
      leaf.visible = k > 0 && k < 1;
      leaf.rotation.y = -0.16 - k * (Math.PI - 0.32);
    },
  },
  notebook: {
    mount: "both", work: "notebook", use: "focus", pageZ: 0.034, grips: [[-0.28, -0.1, -0.01], [0.28, -0.1, -0.01]],
    read: [-0.7, 0, 0], readY: 1.48, readZ: 1.0, hug: [0.08, 0, 0], hugY: 1.28, hugZ: 1.0, offer: [0.15, 0, 0],
    build() {
      const page = paperMat("notebook-page", 256, 320, (g) => {
        g.fillStyle = css(C.cream); g.fillRect(0, 0, 256, 320);
        // the ruled lines match the lines the pencil writes on (motion.js WRITE.lines)
        const y = (v) => 160 - (v / 0.58) * 320 + 6;
        lines(g, 18, 238, [0.19, 0.11, 0.03, -0.05, -0.13, -0.21].map(y), "#bcd4e4", 3);
        g.strokeStyle = "#f2a59d"; g.lineWidth = 3; g.beginPath(); g.moveTo(40, 0); g.lineTo(40, 320); g.stroke();
        g.fillStyle = css(C.mint); g.beginPath(); g.arc(205, 36, 13, 0, TAU); g.fill();
      });
      const g = group(rbox(0.52, 0.64, 0.03, 0.02, glossy(C.teal), [0, 0, 0]), rbox(0.48, 0.6, 0.02, 0.01, mat(C.cream, { roughness: 0.9, clearcoat: 0 }), [0, -0.005, 0.022]));
      g.add(plane(0.46, 0.58, page, [0, -0.005, 0.0335]));
      for (let i = 0; i < 7; i++) g.add(torus(0.032, 0.008, metal(C.steel), [-0.18 + i * 0.06, 0.305, 0.012], [0, Math.PI / 2, 0]));
      // the lines it writes: drawn out as the pencil moves along them
      const ink = mat(C.ink, { roughness: 0.6, clearcoat: 0 });
      g.userData.ink = [0.11, 0.03, -0.05, -0.13].map((y, i) => {
        const pts = []; for (let k = 0; k <= 40; k++) { const x = -0.15 + 0.3 * k / 40; pts.push([x, y + 0.012 * Math.sin(x * 60 + i) + 0.006 * Math.sin(x * 23 + i * 2), 0.037]); }
        const m = tube(pts, 0.0055, ink, 80); m.castShadow = false; g.add(m);
        m.userData.full = m.geometry.index.count; m.geometry.setDrawRange(0, 0);
        return m;
      });
      return g;
    },
    tick(g, t, c) {
      const ink = g.userData.ink;
      if (c.tip && c.writing) {
        const done = c.tip.line + (c.tip.lift ? 1 : c.tip.k);
        g.userData.done = Math.max(g.userData.done || 0, done);
        if (done < (g.userData.done || 0) - 2) g.userData.done = done;
      } else if (c.still) g.userData.done = 1.55;
      const d = g.userData.done || 0;
      ink.forEach((m, i) => { const k = Math.max(0, Math.min(1, d - i)); m.geometry.setDrawRange(0, Math.floor(k * 40) * 2 * 8 * 3); });
    },
  },
  pencil: {
    mount: "hand", work: "pencil", use: "write", scale: 1.15, tip: 0.31, grips: [null, [0, -0.06, 0]], carry: [0.5, 0, -0.35], show: [0.2, 0, -0.5],
    build() {
      return group(
        cyl(0.032, 0.032, 0.44, glossy(0xf2c14e), [0, 0.04, 0], null, 6),
        cyl(0.002, 0.032, 0.085, mat(0xe7c39a, { roughness: 0.8 }), [0, -0.2225, 0], null, 6),
        cyl(0.002, 0.012, 0.03, mat(C.ink), [0, -0.252, 0], null, 6),
        cyl(0.034, 0.034, 0.05, metal(C.steel), [0, 0.285, 0]),
        cyl(0.033, 0.033, 0.05, glossy(C.coral), [0, 0.33, 0]),
        ball(0.033, glossy(C.coral), [0, 0.355, 0], [1, 0.5, 1]),
      );
    },
  },
  tablet: {
    mount: "both", work: "read", use: "read", scale: 1.2, grips: [[-0.35, -0.08, -0.02], [0.35, -0.08, -0.02]],
    read: [-0.62, 0, 0], readY: 1.5, readZ: 1.0, hug: [0.06, 0, 0], offer: [0.12, 0, 0],
    build() {
      const screen = screenMat("tablet", 320, 220, (g) => {
        g.fillStyle = "#18303a"; g.fillRect(0, 0, 320, 220);
        g.fillStyle = css(C.mint); g.fillRect(16, 14, 288, 30);
        g.fillStyle = "#18303a"; g.font = "bold 20px sans-serif"; g.fillText("✳ Daedalus", 26, 36);
        [0, 1, 2].forEach((i) => {
          const y = 60 + i * 50;
          g.fillStyle = "#24444f"; g.beginPath(); g.roundRect(16, y, 288, 40, 8); g.fill();
          g.strokeStyle = i < 2 ? css(C.mint) : "#7d9aa6"; g.lineWidth = 4; g.strokeRect(28, y + 10, 20, 20);
          if (i < 2) { g.beginPath(); g.moveTo(31, y + 20); g.lineTo(37, y + 27); g.lineTo(46, y + 12); g.stroke(); }
          g.fillStyle = "#cfe9e1"; g.fillRect(60, y + 16, 150 - i * 30, 8);
        });
      });
      return group(rbox(0.66, 0.48, 0.035, 0.035, glossy(C.charcoal)), plane(0.58, 0.4, screen, [0, 0.005, 0.0185]), ball(0.012, glow(C.mint), [0, -0.218, 0.018]));
    },
  },
  laptop: {
    mount: "ground", work: "type", use: "focus", ground: { body: [0, 0, 0.98, 0], head: [0, 0, 1.3, 0] }, light: true,
    build() {
      const code = screenMat("laptop", 320, 200, (g) => {
        g.fillStyle = "#132a33"; g.fillRect(0, 0, 320, 200);
        const cols = [css(C.mint), "#8fc4ee", "#f2c97a", "#cfe9e1"];
        for (let i = 0; i < 9; i++) { g.fillStyle = cols[(i * 7) % 4]; g.fillRect(20 + (i % 3) * 16, 18 + i * 20, 90 + ((i * 53) % 130), 8); }
      });
      const keys = paperMat("keys", 256, 128, (g) => {
        g.fillStyle = "#2a3238"; g.fillRect(0, 0, 256, 128);
        g.fillStyle = "#47525a";
        for (let r = 0; r < 4; r++) for (let k = 0; k < 12; k++) g.fillRect(8 + k * 20, 10 + r * 22, 16, 16);
        g.fillRect(60, 98, 136, 16);
      });
      const g = new THREE.Group();
      // a small wooden desk the laptop stands on
      g.add(rbox(0.96, 0.06, 0.52, 0.02, mat(C.wood, { roughness: 0.6 }), [0, 0.99, 0]));
      for (const [x, z] of [[-0.41, -0.2], [0.41, -0.2], [-0.41, 0.2], [0.41, 0.2]]) g.add(cyl(0.028, 0.022, 0.96, mat(C.woodDark), [x, 0.48, z]));
      g.add(rbox(0.62, 0.032, 0.42, 0.012, metal(0x8d9aa3), [0, 1.036, 0.02]));
      g.add(plane(0.54, 0.24, keys, [0, 1.0535, -0.03], [-Math.PI / 2, 0, 0]));
      // the lid opens toward the mascot (behind), its back with the brand mark toward you
      const lid = new THREE.Group(); lid.position.set(0, 1.05, 0.22); lid.rotation.x = 0.26;
      lid.add(rbox(0.62, 0.42, 0.022, 0.012, metal(0x8d9aa3), [0, 0.21, 0]));
      lid.add(plane(0.56, 0.36, code, [0, 0.215, -0.0125], [0, Math.PI, 0]));
      const mark = brandMark(0.07, glow(C.mint)); mark.position.set(0, 0.22, 0.013); lid.add(mark);
      g.add(lid);
      // the screen lights the face a little
      const light = new THREE.PointLight(0x9ff5df, 0.5, 2.6, 2); light.position.set(0, 1.35, -0.25); g.add(light);
      return g;
    },
  },
  calendar: {
    mount: "hand", hand: 0, work: "present", use: "point", scale: 1.25, grips: [[0.17, -0.28, -0.03], null], carry: [0, 0.35, 0], show: [0.05, 0.42, 0.06], carryAt: [-0.86, 1.3, 0.62],
    build() {
      const now = new Date();
      const month = now.toLocaleDateString("ru", { month: "long" });
      const face = paperMat(`calendar-${now.getDate()}`, 256, 256, (g) => {
        g.fillStyle = css(C.cream); g.fillRect(0, 0, 256, 256);
        g.fillStyle = css(C.ink); g.font = "bold 132px sans-serif"; g.textAlign = "center"; g.fillText(String(now.getDate()), 128, 168);
        g.fillStyle = "#9a6b5a"; g.font = "bold 30px sans-serif"; g.fillText(month, 128, 218);
        g.fillStyle = css(C.mint); g.beginPath(); g.arc(214, 40, 12, 0, TAU); g.fill();
      });
      const g = group(rbox(0.46, 0.5, 0.03, 0.02, mat(C.cream, { roughness: 0.8 }), [0, -0.03, 0]), rbox(0.46, 0.14, 0.04, 0.02, glossy(C.coral), [0, 0.2, 0.004]));
      g.add(plane(0.4, 0.36, face, [0, -0.06, 0.016]));
      for (const x of [-0.12, 0.12]) g.add(torus(0.035, 0.01, metal(C.gold), [x, 0.27, 0.0], [0, Math.PI / 2, 0]));
      return g;
    },
  },
  scroll: {
    mount: "both", work: "read", use: "read", grips: [[-0.44, 0, 0], [0.44, 0, 0]],
    read: [-0.55, 0, 0], readY: 1.52, readZ: 1.0, hug: [0.05, 0, 0], offer: [0.12, 0, 0],
    build() {
      const text = paperMat("scroll", 320, 192, (g) => {
        g.fillStyle = "#f3e2c0"; g.fillRect(0, 0, 320, 192);
        g.fillStyle = "#9a6b5a"; g.fillRect(90, 22, 140, 12);
        for (let i = 0; i < 5; i++) scribble(g, 40, 280 - (i % 2) * 50, 62 + i * 24, i * 2, "#7e8f99", 5);
        g.fillStyle = css(C.coral); g.beginPath(); g.arc(250, 160, 14, 0, TAU); g.fill();
      });
      const g = group(plane(0.74, 0.44, text, [0, 0, 0.0]));
      g.children[0].material = g.children[0].material.clone(); g.children[0].material.side = THREE.DoubleSide;
      for (const sx of [-1, 1]) {
        g.add(cyl(0.045, 0.045, 0.5, mat(0xead6b0, { roughness: 0.8 }), [sx * 0.39, 0, 0]));
        g.add(cyl(0.022, 0.022, 0.62, mat(C.woodDark), [sx * 0.39, 0, 0]));
        for (const sy of [-1, 1]) g.add(ball(0.04, metal(C.gold), [sx * 0.39, sy * 0.32, 0]));
      }
      return g;
    },
  },
  map: {
    mount: "both", work: "read", use: "read", grips: [[-0.46, -0.08, -0.02], [0.46, -0.08, -0.02]],
    read: [-0.62, 0, 0], readY: 1.5, readZ: 1.0, hug: [0.06, 0, 0], offer: [0.12, 0, 0],
    build() {
      const art = canvasTexture("map", 400, 250, (g) => {
        g.fillStyle = "#e9dcb6"; g.fillRect(0, 0, 400, 250);
        g.fillStyle = "#9fd0a8"; g.beginPath(); g.ellipse(120, 150, 110, 70, 0.3, 0, TAU); g.fill();
        g.beginPath(); g.ellipse(300, 70, 80, 45, -0.2, 0, TAU); g.fill();
        g.fillStyle = "#93c6e8"; g.beginPath(); g.ellipse(290, 190, 70, 35, 0.1, 0, TAU); g.fill();
        g.strokeStyle = css(C.coral); g.lineWidth = 6; g.setLineDash([12, 10]); g.beginPath(); g.moveTo(40, 220); g.bezierCurveTo(120, 60, 220, 220, 330, 70); g.stroke();
        g.setLineDash([]); g.lineWidth = 9; g.beginPath(); g.moveTo(320, 60); g.lineTo(340, 80); g.moveTo(340, 60); g.lineTo(320, 80); g.stroke();
      });
      const g = new THREE.Group();
      // folded like an accordion: four panels at alternating angles
      for (let i = 0; i < 4; i++) {
        const geo = new THREE.PlaneGeometry(0.235, 0.5);
        const uv = geo.attributes.uv; for (let k = 0; k < uv.count; k++) uv.setX(k, (i + uv.getX(k)) / 4);
        const p = mesh(geo, mat(0xffffff, { map: art, roughness: 0.85, clearcoat: 0, side: THREE.DoubleSide }), [-0.3525 + i * 0.235, 0, i % 2 ? 0.02 : 0], [0, i % 2 ? -0.17 : 0.17, 0]);
        g.add(p);
      }
      g.add(group(cyl(0.006, 0.006, 0.14, metal(C.steel), [0, 0.06, 0]), ball(0.03, glossy(C.coral), [0, 0.13, 0])));
      g.children[4].position.set(0.31, 0.1, 0.05); g.children[4].rotation.x = 0.5;
      return g;
    },
  },
  hourglass: {
    mount: "ground", use: "listen", watch: true, scale: 1.3, ground: { body: [-1.38, 0, 0.62, 0.3], head: [-1.55, 0, 0.6, 0.3] },
    build() {
      const g = new THREE.Group(), body = new THREE.Group(); body.position.y = 0.46; g.add(body);
      for (const y of [-0.43, 0.43]) body.add(cyl(0.24, 0.24, 0.05, mat(C.wood, { roughness: 0.6 }), [0, y, 0]));
      for (let i = 0; i < 3; i++) { const a = (i / 3) * TAU + 0.5; body.add(cyl(0.018, 0.018, 0.82, mat(C.woodDark), [Math.cos(a) * 0.2, 0, Math.sin(a) * 0.2])); }
      body.add(lathe([[0.02, -0.4], [0.15, -0.3], [0.17, -0.18], [0.1, -0.06], [0.026, 0], [0.1, 0.06], [0.17, 0.18], [0.15, 0.3], [0.02, 0.4]], glass(0xe6f6ff, 0.3)));
      const sandM = mat(C.sand, { roughness: 0.9, clearcoat: 0 });
      const top = mesh(new THREE.ConeGeometry(0.14, 0.2, 24), sandM, [0, 0.09, 0], [Math.PI, 0, 0]);
      const bottom = mesh(new THREE.ConeGeometry(0.15, 0.16, 24), sandM, [0, -0.32, 0]);
      const stream = cyl(0.007, 0.007, 0.3, sandM, [0, -0.15, 0]);
      body.add(top, bottom, stream);
      g.userData = { body, top, bottom, stream };
      return g;
    },
    tick(g, t, c) {
      const { body, top, bottom, stream } = g.userData;
      const per = 12, ph = c.still ? 0.45 : (t % per) / per;
      const flow = Math.min(1, ph / 0.85), flip = Math.max(0, (ph - 0.88) / 0.12);
      const k = 1 - flow;
      top.scale.set(0.3 + 0.7 * Math.sqrt(k), Math.max(0.02, k), 0.3 + 0.7 * Math.sqrt(k));
      top.position.y = 0.1;
      bottom.scale.set(0.35 + 0.65 * Math.sqrt(flow), Math.max(0.02, flow), 0.35 + 0.65 * Math.sqrt(flow));
      bottom.position.y = -0.4 + 0.08 * flow;
      stream.visible = flow < 0.99 && flip === 0;
      body.rotation.z = flip * Math.PI;
    },
  },
  clock: {
    mount: "ground", use: "point", watch: true, scale: 1.25, ground: { body: [-1.38, 0, 0.62, 0.38], head: [-1.55, 0, 0.6, 0.35] },
    build() {
      const face = paperMat("clock-face", 256, 256, (g) => {
        g.fillStyle = css(C.cream); g.beginPath(); g.arc(128, 128, 128, 0, TAU); g.fill();
        g.fillStyle = css(C.ink);
        for (let i = 0; i < 12; i++) { const a = (i / 12) * TAU; g.save(); g.translate(128 + Math.sin(a) * 110, 128 - Math.cos(a) * 110); g.rotate(a); g.fillRect(-3, 0, 6, i % 3 ? 10 : 18); g.restore(); }
        g.font = "bold 34px sans-serif"; g.textAlign = "center"; g.textBaseline = "middle";
        g.fillText("12", 128, 52); g.fillText("3", 206, 128); g.fillText("6", 128, 206); g.fillText("9", 50, 128);
      });
      const g = new THREE.Group(), head = new THREE.Group(); head.position.y = 0.42; g.add(head);
      head.add(cyl(0.3, 0.3, 0.16, glossy(C.coral), [0, 0, 0], [Math.PI / 2, 0, 0], 40));
      head.add(torus(0.29, 0.035, metal(C.gold), [0, 0, 0.08]));
      head.add(mesh(new THREE.CircleGeometry(0.265, 40), face, [0, 0, 0.082]));
      const hand = (w, l, color, z) => { const p = new THREE.Group(); p.position.z = z; p.add(rbox(w, l, 0.012, 0.005, mat(color), [0, l / 2 - 0.03, 0])); head.add(p); return p; };
      const hh = hand(0.03, 0.15, C.ink, 0.092), mm = hand(0.022, 0.21, C.ink, 0.1), ss = hand(0.01, 0.23, C.coral, 0.108);
      head.add(ball(0.022, metal(C.gold), [0, 0, 0.11]));
      for (const sx of [-1, 1]) {
        head.add(ball(0.11, metal(C.gold), [sx * 0.19, 0.27, -0.01], [1, 0.8, 1]));
        head.add(cyl(0.025, 0.025, 0.2, mat(C.charcoal), [sx * 0.17, -0.29, 0], [0, 0, sx * 0.5]));
      }
      head.add(cyl(0.015, 0.015, 0.12, metal(C.gold), [0, 0.33, 0]));
      g.userData = { hh, mm, ss };
      return g;
    },
    tick(g, t, c) {
      const d = new Date(), s = c.still ? 0 : d.getSeconds(), m = d.getMinutes() + s / 60, h = (d.getHours() % 12) + m / 60;
      g.userData.ss.rotation.z = -(s / 60) * TAU; g.userData.mm.rotation.z = -(m / 60) * TAU; g.userData.hh.rotation.z = -(h / 12) * TAU;
    },
  },
  compass: {
    mount: "hand", work: "compass", use: "focus", scale: 1.3, grips: [null, [0, -0.2, -0.02]], carry: [0.3, 0, 0], read: [0.62, 0, 0], show: [0.9, 0, 0],
    build() {
      const face = paperMat("compass", 256, 256, (g) => {
        g.fillStyle = css(C.cream); g.beginPath(); g.arc(128, 128, 128, 0, TAU); g.fill();
        g.strokeStyle = "#9fb0b9"; g.lineWidth = 3; g.beginPath(); g.arc(128, 128, 100, 0, TAU); g.stroke();
        g.fillStyle = css(C.ink); g.font = "bold 38px sans-serif"; g.textAlign = "center"; g.textBaseline = "middle";
        g.fillText("С", 128, 44); g.fillText("Ю", 128, 214); g.fillText("З", 42, 128); g.fillText("В", 214, 128);
      });
      const g = group(cyl(0.2, 0.21, 0.07, metal(C.gold), [0, 0, 0], null, 40));
      g.add(mesh(new THREE.CircleGeometry(0.17, 40), face, [0, 0.036, 0], [-Math.PI / 2, 0, 0]));
      g.add(torus(0.05, 0.014, metal(C.gold), [0, 0, -0.22], [Math.PI / 2, 0, 0]));
      const needle = new THREE.Group(); needle.position.y = 0.046;
      const n1 = mesh(new THREE.ConeGeometry(0.03, 0.14, 4), glossy(C.coral), [0, 0, -0.07], [-Math.PI / 2, 0, 0]);
      const n2 = mesh(new THREE.ConeGeometry(0.03, 0.14, 4), glossy(C.white), [0, 0, 0.07], [Math.PI / 2, 0, 0]);
      needle.add(n1, n2, ball(0.016, metal(C.gold)));
      g.add(needle);
      g.add(cyl(0.175, 0.175, 0.004, glass(), [0, 0.06, 0]));
      g.userData.needle = needle;
      return g;
    },
    tick(g, t, c) { g.userData.needle.rotation.y = c.still ? 0.2 : 0.25 * Math.sin(t * 1.3) * Math.exp(-((t % 6) * 0.4)) + 0.1 * Math.sin(t * 0.4); },
  },
  magnifier: {
    mount: "hand", work: "inspect", use: "focus", grips: [null, [0, -0.5, 0]], carry: [0.25, 0, -0.3], show: [0.15, 0, -0.25], inspect: [0.08, 0, 0.45],
    build() {
      return group(
        torus(0.16, 0.028, metal(C.gold)),
        mesh(new THREE.CircleGeometry(0.15, 40), glass(0xcfeeff, 0.22)),
        cyl(0.03, 0.03, 0.06, metal(C.gold), [0, -0.205, 0]),
        cyl(0.032, 0.04, 0.34, mat(C.woodDark, { roughness: 0.5 }), [0, -0.4, 0]),
        ball(0.04, mat(C.woodDark), [0, -0.575, 0]),
      );
    },
  },
  gear: {
    mount: "float", use: "think", follow: "head", float: { body: [1.05, 4.62, 0.3], head: [1.5, 1.72, 0.2] },
    build() {
      const g = new THREE.Group(), cloud = new THREE.Group(); g.add(cloud);
      const puff = mat(0xf7fbfa, { roughness: 0.9, clearcoat: 0 });
      [[0, 0, 0, 0.26], [-0.26, -0.04, 0, 0.2], [0.27, -0.03, 0, 0.21], [-0.1, 0.16, -0.02, 0.2], [0.14, 0.15, -0.02, 0.19], [0, -0.1, 0.05, 0.2]].forEach(([x, y, z, r]) => cloud.add(ball(r, puff, [x, y, z], [1, 0.85, 0.75])));
      const dots = [[-0.42, -0.38, 0.1, 0.07], [-0.6, -0.6, 0.15, 0.045]].map(([x, y, z, r]) => { const d = ball(r, puff, [x, y, z]); g.add(d); return d; });
      const shape = new THREE.Shape(); const n = 10;
      for (let i = 0; i < n * 2; i++) { const a0 = (i / (n * 2)) * TAU, a1 = ((i + 1) / (n * 2)) * TAU, r = i % 2 ? 0.13 : 0.165; if (!i) shape.moveTo(Math.cos(a0) * r, Math.sin(a0) * r); shape.lineTo(Math.cos(a0) * r, Math.sin(a0) * r); shape.lineTo(Math.cos(a1) * r, Math.sin(a1) * r); }
      const hole = new THREE.Path(); hole.absarc(0, 0, 0.045, 0, TAU, true); shape.holes.push(hole);
      const gear = extrude(shape, 0.05, metal(C.gold), 0.01, [0, 0.02, 0.24]);
      g.add(gear);
      g.userData = { gear, cloud, dots };
      return g;
    },
    tick(g, t, c) {
      const { gear, cloud, dots } = g.userData;
      if (c.still) return;
      gear.rotation.z = -t * 0.9 + Math.sin(t * 6) * 0.02;
      cloud.position.y = 0.03 * Math.sin(t * 1.3);
      dots.forEach((d, i) => d.scale.setScalar(1 + 0.12 * Math.sin(t * 2.4 - i)));
    },
  },
  key: {
    mount: "hand", work: "show", use: "offer", scale: 1.35, grips: [null, [0, 0.16, 0]], carry: [0, 0, 0.35], show: [-Math.PI / 2 + 0.25, 0, 0], raise: [0, 0, 0.3],
    build() {
      const spin = new THREE.Group();
      spin.add(torus(0.1, 0.03, metal(C.gold), [0, 0.16, 0]));
      spin.add(ball(0.03, metal(C.gold), [0, 0.16, 0]));
      spin.add(cyl(0.026, 0.026, 0.44, metal(C.gold), [0, -0.14, 0]));
      spin.add(rbox(0.1, 0.05, 0.03, 0.01, metal(C.gold), [0.05, -0.31, 0]), rbox(0.07, 0.05, 0.03, 0.01, metal(C.gold), [0.035, -0.22, 0]));
      const g = group(spin); g.userData.spin = spin;
      return g;
    },
    tick(g, t, c) { g.userData.spin.rotation.y = c.still || c.action !== "offer" ? 0 : Math.max(0, Math.sin(t * 1.4)) ** 2 * 1.4; },
  },
  wrench: {
    mount: "hand", work: "tinker", use: "focus", scale: 1.3, grips: [null, [0, -0.24, 0]], carry: [0.2, 0, -0.3], tinker: [0, 0.3, 1.25],
    build() {
      // an open-ended spanner: a handle and a jaw with its mouth up
      const jaw = new THREE.Shape([new THREE.Vector2(-0.1, -0.06), new THREE.Vector2(0.1, -0.06), new THREE.Vector2(0.11, 0.08), new THREE.Vector2(0.045, 0.08), new THREE.Vector2(0.035, 0.0), new THREE.Vector2(-0.035, 0.0), new THREE.Vector2(-0.045, 0.08), new THREE.Vector2(-0.11, 0.08)]);
      return group(rbox(0.075, 0.42, 0.035, 0.03, metal(C.steel), [0, -0.05, 0]), extrude(jaw, 0.035, metal(C.steel), 0.012, [0, 0.21, 0]), rbox(0.05, 0.18, 0.04, 0.02, glossy(C.coral), [0, -0.18, 0]));
    },
  },
  crystal: {
    mount: "float", use: "offer", follow: "body", float: { body: [0, 1.92, 1.22], head: [0, -1.05, 0.9] },
    build() {
      const core = new THREE.MeshPhysicalMaterial({ color: C.mint, emissive: C.mint, emissiveIntensity: 0.5, roughness: 0.15, clearcoat: 1, flatShading: true });
      const gem = mesh(new THREE.LatheGeometry([[0, -0.3], [0.15, -0.1], [0.15, 0.12], [0, 0.34]].map(([x, y]) => new THREE.Vector2(x, y)), 6), core);
      const sparks = [0, 1, 2].map((i) => { const s = mesh(new THREE.OctahedronGeometry(0.035), glow(0xe8fff8)); s.userData.a = (i / 3) * TAU; return s; });
      const light = new THREE.PointLight(0x7ff5d8, 0.9, 2.4, 2);
      const g = group(gem, ...sparks, light);
      g.userData = { gem, sparks, core };
      return g;
    },
    tick(g, t, c) {
      const { gem, sparks, core } = g.userData, T = c.still ? 0.8 : t;
      gem.rotation.y = T * 0.6; gem.position.y = c.still ? 0 : 0.04 * Math.sin(T * 1.6);
      core.emissiveIntensity = 0.45 + 0.15 * Math.sin(T * 2.2);
      sparks.forEach((s) => { const a = s.userData.a + T * 1.3; s.position.set(Math.cos(a) * 0.3, 0.12 * Math.sin(a * 2), Math.sin(a) * 0.3); });
    },
  },
  mug: {
    mount: "hand", use: "sip", scale: 1.12, grips: [[-0.25, -0.01, 0.0], [0.28, 0.0, 0]], carry: [0, 0.75, 0], show: [0, -0.4, 0], raise: [0, 0.5, 0], sipYaw: 0.15,
    build() {
      const g = new THREE.Group();
      g.add(lathe([[0, -0.17], [0.15, -0.17], [0.17, -0.15], [0.18, 0.17], [0.165, 0.17], [0.155, -0.13], [0, -0.13]], mat(C.cream, { roughness: 0.4, clearcoat: 0.6, side: THREE.DoubleSide })));
      g.add(cyl(0.183, 0.18, 0.07, glossy(C.mint), [0, 0.02, 0], null, 36));
      g.add(cyl(0.152, 0.152, 0.01, mat(0x8a5634, { roughness: 0.2, clearcoat: 1 }), [0, 0.12, 0], null, 32));
      g.add(torus(0.085, 0.028, mat(C.cream, { roughness: 0.4, clearcoat: 0.6 }), [0.2, 0.0, 0], [0, 0, 0], Math.PI * 1.25));
      g.children[3].rotation.z = -Math.PI * 0.62;
      const tick = steam(g, [0, 0.2, 0], 3, 0.06, 0.45);
      g.userData.steam = tick;
      return g;
    },
    // no steam while the mug is at the mouth: it would rise through the face
    tick(g, t, c) { g.userData.steam(t, c.still, c.action === "sip" && sipping(c.e, c.still)); },
  },
  teapot: {
    mount: "ground", use: "sip", companion: "mug", ground: { body: [-1.3, 0, 0.6, 0.55], head: [-1.5, 0, 0.6, 0.5] },
    build() {
      const g = new THREE.Group();
      g.add(cyl(0.3, 0.32, 0.04, mat(C.wood, { roughness: 0.6 }), [0, 0.02, 0], null, 40));
      g.add(lathe([[0, 0.04], [0.16, 0.05], [0.25, 0.14], [0.27, 0.27], [0.22, 0.4], [0.12, 0.45], [0, 0.45]], glossy(C.coral)));
      g.add(lathe([[0, 0.45], [0.13, 0.45], [0.11, 0.5], [0.04, 0.52], [0, 0.52]], glossy(C.cream)));
      g.add(ball(0.04, metal(C.gold), [0, 0.55, 0]));
      g.add(tube([[0.22, 0.2, 0], [0.36, 0.28, 0], [0.42, 0.42, 0], [0.47, 0.46, 0]], 0.04, glossy(C.coral), 20));
      g.add(torus(0.12, 0.028, glossy(C.cream), [-0.27, 0.28, 0], [0, 0, 0], Math.PI * 1.1));
      g.children[5].rotation.z = Math.PI * 0.45;
      g.userData.steam = steam(g, [0.5, 0.5, 0], 3, 0.045, 0.4);
      return g;
    },
    tick(g, t, c) { g.userData.steam(t, c.still); },
  },
  plant: {
    mount: "both", work: "hug", use: "offer", grips: [[-0.24, 0.1, 0], [0.24, 0.1, 0]],
    hug: [0.05, 0, 0], hugY: 1.25, hugZ: 1.0, offer: [0.08, 0, 0], offerY: 1.42, offerZ: 0.98, read: [0.0, 0, 0], tuck: [-0.64, 0.88, 0.72, 0.05, -0.4, 0.05],
    build() {
      const g = new THREE.Group();
      g.add(lathe([[0, -0.02], [0.15, -0.02], [0.2, 0.26], [0.22, 0.27], [0.22, 0.31], [0.19, 0.31], [0.16, 0.05], [0, 0.05]], mat(C.terracotta, { roughness: 0.7 })));
      g.add(cyl(0.185, 0.185, 0.02, mat(0x4a3424, { roughness: 1 }), [0, 0.28, 0]));
      const stem = tube([[0, 0.28, 0], [0.02, 0.45, 0], [-0.02, 0.6, 0.01], [0.01, 0.72, 0]], 0.016, mat(C.leafDark), 20);
      g.add(stem);
      const leafShape = new THREE.Shape(); leafShape.moveTo(0, 0); leafShape.quadraticCurveTo(0.1, 0.06, 0.2, 0); leafShape.quadraticCurveTo(0.1, -0.06, 0, 0);
      const leaves = [[0.02, 0.45, 0.4], [-0.02, 0.58, Math.PI - 0.5], [0.01, 0.7, 0.9]].map(([x, y, a], i) => {
        const p = new THREE.Group(); p.position.set(x, y, 0); p.rotation.set(0, 0.4 * i, a);
        p.add(extrude(leafShape, 0.012, mat(i === 2 ? C.leaf : C.leafDark, { roughness: 0.5, clearcoat: 0.4 }), 0.006));
        g.add(p); return p;
      });
      g.add(ball(0.035, glossy(0xf7a6b5), [0.01, 0.75, 0]));
      g.userData.leaves = leaves;
      return g;
    },
    tick(g, t, c) { if (!c.still) g.userData.leaves.forEach((p, i) => { p.rotation.x = 0.12 * Math.sin(t * 1.4 + i); }); },
  },
  lantern: {
    mount: "hand", work: "show", use: "offer", grips: [null, [0, 0.4, 0]], carry: [0, 0, 0], show: [0, 0, 0], raise: [0, 0, 0], light: true,
    build() {
      const g = new THREE.Group(), body = new THREE.Group(); g.add(body); body.position.y = 0.4;
      body.add(torus(0.06, 0.014, metal(C.gold), [0, 0, 0]));
      body.add(cyl(0.05, 0.17, 0.11, metal(0x8b6a3e), [0, -0.1, 0]));
      for (let i = 0; i < 4; i++) { const a = (i / 4) * TAU + Math.PI / 4; body.add(cyl(0.012, 0.012, 0.38, metal(0x8b6a3e), [Math.cos(a) * 0.12, -0.34, Math.sin(a) * 0.12])); }
      body.add(cyl(0.115, 0.115, 0.34, glass(0xffe3b0, 0.25), [0, -0.34, 0]));
      body.add(cyl(0.16, 0.15, 0.05, metal(0x8b6a3e), [0, -0.54, 0]));
      const flame = ball(0.05, glow(0xffc46b, 1.4), [0, -0.33, 0], [0.8, 1.35, 0.8]);
      body.add(cyl(0.035, 0.035, 0.1, mat(C.cream), [0, -0.46, 0]));
      body.add(flame);
      const light = new THREE.PointLight(0xffb46a, 2.4, 4.2, 2); light.position.set(0, -0.33, 0); body.add(light);
      // the body hangs from the ring and sways a little behind the hand
      body.position.set(0, 0, 0);
      g.userData = { body, flame, light };
      return g;
    },
    tick(g, t, c) {
      const { body, flame, light } = g.userData;
      const f = c.still ? 1 : 1 + 0.08 * Math.sin(t * 11) + 0.05 * Math.sin(t * 17.3);
      flame.scale.set(0.8, 1.35 * f, 0.8); light.intensity = 2.4 * f;
      body.rotation.z = c.still ? 0 : 0.07 * Math.sin(t * 1.9);
      body.rotation.x = c.still ? 0 : 0.04 * Math.sin(t * 1.3 + 1);
    },
  },
  pillow: {
    mount: "both", work: "hug", use: "sleep", grips: [[-0.37, -0.02, -0.02], [0.37, -0.02, -0.02]],
    hug: [0.06, 0, 0], hugY: 1.3, hugZ: 0.93, sleepY: 1.4, sleepZ: 0.84, offer: [0.1, 0, 0], read: [0.0, 0, 0],
    build() {
      const geo = new THREE.SphereGeometry(1, 40, 28), p = geo.attributes.position;
      for (let i = 0; i < p.count; i++) {
        const f = (v) => Math.sign(v) * Math.pow(Math.abs(v), 0.55);
        p.setXYZ(i, f(p.getX(i)) * 0.38, f(p.getY(i)) * 0.25 * (1 - 0.18 * p.getX(i) ** 2), f(p.getZ(i)) * 0.12 * (1 - 0.5 * p.getX(i) ** 2 - 0.3 * p.getY(i) ** 2));
      }
      geo.computeVertexNormals();
      const g = group(mesh(geo, mat(C.lilac, { roughness: 0.85, clearcoat: 0, sheen: 1, sheenColor: new THREE.Color(0xe6dcff) })));
      g.add(ball(0.025, mat(C.cream), [0, 0, 0.122]));
      for (const [x, y] of [[-0.36, 0.24], [0.36, 0.24], [-0.36, -0.24], [0.36, -0.24]]) g.add(ball(0.03, mat(C.lilac, { roughness: 0.9 }), [x, y, 0], [1, 1, 0.6]));
      return g;
    },
  },
  blanket: {
    mount: "wear", use: "sip", foldWings: true,
    build(variant) {
      const plaid = canvasTexture("plaid", 256, 256, (g) => {
        g.fillStyle = "#f4e3c8"; g.fillRect(0, 0, 256, 256);
        g.globalAlpha = 0.55; g.fillStyle = css(C.coral); for (let i = 0; i < 4; i++) { g.fillRect(i * 64 + 8, 0, 18, 256); g.fillRect(0, i * 64 + 8, 256, 18); }
        g.globalAlpha = 0.5; g.fillStyle = css(C.teal); for (let i = 0; i < 4; i++) { g.fillRect(i * 64 + 40, 0, 6, 256); g.fillRect(0, i * 64 + 40, 256, 6); }
      });
      plaid.wrapS = plaid.wrapT = THREE.RepeatWrapping; plaid.repeat.set(3, 1);
      const cloth = mat(0xffffff, { map: plaid, roughness: 0.95, clearcoat: 0, sheen: 1, sheenColor: new THREE.Color(0xffe8d0), side: THREE.DoubleSide });
      const g = new THREE.Group();
      if (variant === "head") {
        const hood = mesh(new THREE.LatheGeometry([[0.3, 1.02], [0.75, 0.92], [1.08, 0.6], [1.16, 0.1], [1.18, -0.45]].map(([x, y]) => new THREE.Vector2(x, y)), 40, 0.95, TAU - 1.9), cloth);
        hood.scale.set(1, 1, 0.9); g.add(hood);
      } else {
        const shawl = mesh(new THREE.LatheGeometry([[0.55, 2.02], [0.72, 1.86], [0.88, 1.6], [0.98, 1.36], [1.0, 1.2]].map(([x, y]) => new THREE.Vector2(x, y)), 44, 0.7, TAU - 1.4), cloth);
        shawl.scale.set(1, 1, 0.95); g.add(shawl);
        const roll = mesh(new THREE.TorusGeometry(0.57, 0.07, 10, 40, TAU - 1.6), cloth, [0, 2.0, 0], [Math.PI / 2, 0, Math.PI / 2 + 0.8]);
        roll.scale.set(1, 0.95, 1); g.add(roll);
      }
      return g;
    },
  },
  palette: {
    mount: "hand", hand: 0, work: "paint", use: "write", companion: "brush", grips: [[-0.16, -0.06, -0.04], null], carry: [1.1, 0.35, 0.15],
    build() {
      const s = new THREE.Shape();
      s.moveTo(-0.26, -0.02); s.bezierCurveTo(-0.3, 0.2, 0.1, 0.24, 0.26, 0.12); s.bezierCurveTo(0.34, 0.04, 0.3, -0.16, 0.12, -0.18);
      s.bezierCurveTo(0.02, -0.19, 0.0, -0.1, -0.06, -0.1); s.bezierCurveTo(-0.14, -0.1, -0.24, -0.14, -0.26, -0.02);
      const hole = new THREE.Path(); hole.absarc(-0.16, -0.04, 0.04, 0, TAU, true); s.holes.push(hole);
      const g = group(extrude(s, 0.025, mat(0xd29b63, { roughness: 0.55 }), 0.008));
      [[C.coral, 0.12, 0.12], [C.mint, 0.0, 0.15], [C.sky, -0.12, 0.13], [C.gold, 0.2, 0.0], [C.lilac, 0.16, -0.1]].forEach(([c, x, y]) => g.add(ball(0.045, glossy(c), [x, y, 0.025], [1, 1, 0.45])));
      return g;
    },
  },
  brush: {
    mount: "hand", work: "paint", use: "write", companion: "palette", trail: true, grips: [null, [0, -0.12, 0]], carry: [0.2, 0, -0.4], paint: [0.35, 0, -0.35],
    build() {
      return group(
        cyl(0.03, 0.022, 0.42, glossy(C.teal), [0, -0.12, 0]),
        cyl(0.034, 0.03, 0.08, metal(C.steel), [0, 0.13, 0]),
        mesh(new THREE.SphereGeometry(0.045, 16, 12), mat(0xe9d2a8, { roughness: 0.9 }), [0, 0.2, 0], null, [0.75, 1.5, 0.75]),
        mesh(new THREE.SphereGeometry(0.03, 14, 10), glossy(C.coral), [0, 0.255, 0], null, [0.9, 1.3, 0.9]),
      );
    },
  },
  camera: {
    mount: "both", work: "photo", use: "focus", grips: [[-0.28, -0.04, -0.02], [0.28, -0.04, -0.02]],
    offer: [0.04, 0, 0], read: [0.0, 0, 0], hug: [0.04, 0, 0], hugY: 1.32, readY: 1.6,
    build() {
      const g = new THREE.Group();
      g.add(rbox(0.52, 0.32, 0.22, 0.05, glossy(C.charcoal)));
      g.add(rbox(0.524, 0.1, 0.224, 0.04, mat(C.cream, { roughness: 0.6 }), [0, 0.12, 0]));
      g.add(cyl(0.12, 0.12, 0.08, glossy(0x1d2226), [0.03, -0.02, 0.13], [Math.PI / 2, 0, 0], 32));
      g.add(cyl(0.09, 0.1, 0.06, metal(C.steel), [0.03, -0.02, 0.19], [Math.PI / 2, 0, 0], 32));
      g.add(mesh(new THREE.CircleGeometry(0.075, 32), glossy(0x5a7fa0), [0.03, -0.02, 0.221]));
      g.add(cyl(0.03, 0.03, 0.04, glossy(C.coral), [0.17, 0.18, 0]));
      const flashM = new THREE.MeshBasicMaterial({ color: 0xfff1d0, toneMapped: false });
      g.add(rbox(0.11, 0.07, 0.04, 0.012, flashM, [-0.17, 0.08, 0.115]));
      const light = new THREE.PointLight(0xfff4e0, 0, 4, 2); light.position.set(-0.17, 0.08, 0.4); g.add(light);
      g.userData = { flashM, light };
      return g;
    },
    tick(g, t, c) {
      const f = c.still || c.action !== "focus" ? 0 : Math.max(0, 1 - Math.abs((c.e % 3.4) - 2.5) / 0.12);
      g.userData.flashM.color.setRGB(1 + 3 * f, 0.95 + 3 * f, 0.82 + 3 * f);
      g.userData.light.intensity = 9 * f;
    },
  },
  music: {
    mount: "float", use: "dance", follow: "head", float: { body: [0, 3.0, 0], head: [0, 0.35, 0] },
    build() {
      const note = (color, beamed) => {
        const m = glossy(color), n = new THREE.Group();
        n.add(ball(0.07, m, [0, 0, 0], [1.25, 0.9, 0.7]));
        n.add(cyl(0.012, 0.012, 0.28, m, [0.075, 0.14, 0]));
        if (beamed) { n.add(ball(0.07, m, [0.2, 0.04, 0], [1.25, 0.9, 0.7]), cyl(0.012, 0.012, 0.28, m, [0.275, 0.18, 0]), rbox(0.23, 0.045, 0.03, 0.01, m, [0.175, 0.3, 0], [0, 0, 0.18])); }
        else n.add(tube([[0.075, 0.28, 0], [0.13, 0.22, 0], [0.15, 0.13, 0]], 0.015, m, 12));
        n.children[0].rotation.z = 0.4;
        return n;
      };
      const notes = [note(C.mint, false), note(C.gold, true), note(C.coral, false)];
      const g = group(...notes); g.userData.notes = notes;
      return g;
    },
    tick(g, t, c) {
      // a round orbit wide enough to pass the corners of the boxy head
      const r = c.variant === "head" ? 1.62 : 1.66;
      g.userData.notes.forEach((n, i) => {
        const a = (c.still ? 0.6 : t * 0.55) + (i / 3) * TAU;
        n.position.set(Math.sin(a) * r, 0.25 * i + (c.still ? 0 : 0.12 * Math.sin(t * 2 + i * 2)), Math.cos(a) * r);
        n.rotation.z = c.still ? 0 : 0.2 * Math.sin(t * 2.4 + i);
        n.visible = true;
      });
    },
  },
  star: {
    mount: "hand", work: "show", use: "celebrate", scale: 1.25, grips: [null, [0, -0.3, 0]], carry: [1.15, 0, -0.2], show: [0, 0, 0], raise: [0, 0, -0.2],
    build() {
      const s = extrude(star(0.2, 0.09), 0.05, new THREE.MeshPhysicalMaterial({ color: C.gold, emissive: C.gold, emissiveIntensity: 0.35, metalness: 0.4, roughness: 0.3, clearcoat: 1 }), 0.025, [0, 0.12, 0]);
      const glints = [0, 1].map((i) => { const m = mesh(new THREE.OctahedronGeometry(0.035), glow(0xfff6d8)); m.position.set(i ? 0.22 : -0.2, i ? 0.3 : 0.0, 0.05); return m; });
      const g = group(s, cyl(0.018, 0.018, 0.34, glossy(C.teal), [0, -0.2, 0]), ...glints);
      g.userData = { s, glints };
      return g;
    },
    tick(g, t, c) {
      const { s, glints } = g.userData;
      s.rotation.y = c.still ? 0.3 : Math.sin(t * 1.5) * 0.5;
      glints.forEach((m, i) => m.scale.setScalar(c.still ? 1 : Math.max(0, Math.sin(t * 3 + i * 2.1)) * 1.4));
    },
  },
  microphone: {
    mount: "hand", work: "listen", use: "speak", scale: 1.2, grips: [null, [0, -0.24, 0]], carry: [0.3, 0, -0.2], offer: [1.15, 0, 0.0], speak: [0.12, 0, 0.3], show: [0.6, 0, -0.1],
    build() {
      const grille = canvasTexture("grille", 64, 64, (g) => { g.fillStyle = "#3a4249"; g.fillRect(0, 0, 64, 64); g.strokeStyle = "#5f6a72"; g.lineWidth = 2; for (let i = 0; i < 64; i += 8) { g.beginPath(); g.moveTo(i, 0); g.lineTo(i, 64); g.moveTo(0, i); g.lineTo(64, i); g.stroke(); } });
      grille.wrapS = grille.wrapT = THREE.RepeatWrapping; grille.repeat.set(4, 2);
      return group(
        ball(0.11, mat(0xffffff, { map: grille, metalness: 0.5, roughness: 0.4 }), [0, 0.12, 0]),
        torus(0.104, 0.014, glossy(C.mint), [0, 0.07, 0], [Math.PI / 2, 0, 0]),
        cyl(0.055, 0.04, 0.36, glossy(C.charcoal), [0, -0.12, 0]),
        ball(0.012, glow(C.mint), [0, -0.02, 0.05]),
      );
    },
  },
  headphones: {
    mount: "head", use: "listen", head: [0, 0.02, 0, 0, 0, 0],
    build(variant) {
      const g = new THREE.Group();
      const pts = [];
      for (let i = 0; i <= 24; i++) { const a = (i / 24) * Math.PI, c = Math.cos(a), s = Math.sin(a); pts.push([1.1 * Math.sign(c) * Math.pow(Math.abs(c), 0.42), 0.97 * Math.pow(s, 0.42), 0]); }
      const band = tube(pts, 0.05, glossy(C.charcoal), 60);
      const bandWrap = new THREE.Group(); bandWrap.rotation.x = -0.42; bandWrap.add(band);
      band.add(tube(pts.slice(6, 19).map(([x, y]) => [x * 0.985, y * 0.985 - 0.012, 0.0]), 0.04, glossy(C.mint), 30));
      g.add(bandWrap);
      const cupY = variant === "head" ? 0.22 : 0.02;
      for (const sx of [-1, 1]) {
        const cup = new THREE.Group(); cup.position.set(sx * 1.08, cupY, 0); cup.rotation.z = Math.PI / 2;
        cup.add(cyl(0.25, 0.25, 0.15, glossy(C.charcoal), [0, -sx * 0.02, 0], null, 36));
        cup.add(mesh(new THREE.TorusGeometry(0.2, 0.05, 10, 32), mat(C.mint, { roughness: 0.8 }), [0, sx * 0.075 * -1, 0], [Math.PI / 2, 0, 0]));
        cup.add(cyl(0.12, 0.12, 0.02, glow(C.mint, 0.9), [0, -sx * 0.1, 0], null, 24));
        g.add(cup);
      }
      return g;
    },
  },
  envelope: {
    mount: "both", work: "show", use: "offer", scale: 1.2, grips: [[-0.3, -0.06, -0.02], [0.3, -0.06, -0.02]],
    offer: [0.15, 0, 0], read: [-0.35, 0, 0], hug: [0.06, 0, 0], offerY: 1.52,
    build() {
      const g = group(rbox(0.58, 0.4, 0.03, 0.015, mat(C.cream, { roughness: 0.8 })));
      const flap = new THREE.Shape(); flap.moveTo(-0.28, 0.19); flap.lineTo(0.28, 0.19); flap.lineTo(0, -0.04); flap.closePath();
      g.add(mesh(new THREE.ShapeGeometry(flap), mat(0xefdcbc, { roughness: 0.8 }), [0, 0, 0.0165]));
      g.add(extrude(heartShape(0.045), 0.02, glossy(C.rose), 0.01, [0, -0.02, 0.03]));
      return g;
    },
  },
  plane: {
    mount: "float", use: "point", watch: true, float: { body: [0, 3.0, 0], head: [0, 0.4, 0] },
    build() {
      const geo = new THREE.BufferGeometry();
      // a paper dart: nose at +z, two wings and a keel
      const v = [0, 0, 0.42, -0.3, 0.04, -0.2, 0, 0.0, -0.18, 0, 0, 0.42, 0, 0.0, -0.18, 0.3, 0.04, -0.2, 0, 0, 0.42, 0, -0.1, -0.16, 0, 0.0, -0.18];
      geo.setAttribute("position", new THREE.Float32BufferAttribute(v, 3)); geo.computeVertexNormals();
      const paper = mat(0xfdfbf5, { roughness: 0.8, clearcoat: 0, side: THREE.DoubleSide });
      const dart = mesh(geo, paper);
      const stripe = mesh(new THREE.PlaneGeometry(0.03, 0.4), glow(C.mint, 0.9), [0, 0.012, 0.08], [-Math.PI / 2, 0, 0]);
      const g = group(group(dart, stripe));
      g.userData.dart = g.children[0];
      return g;
    },
    tick(g, t, c) {
      const r = c.variant === "head" ? 1.7 : 1.9, T = c.still ? 1.2 : t * 0.75;
      const x = Math.sin(T) * r, z = Math.cos(T) * r * 0.75, y = 0.22 * Math.sin(T * 2.3);
      const d = g.userData.dart;
      d.position.set(x, y, z);
      d.rotation.set(0.15 * Math.cos(T * 2.3), T + Math.PI / 2, 0, "YXZ");
      d.rotation.z = -0.45;
    },
  },
  bubble: {
    mount: "float", use: "speak", follow: "head", float: { body: [1.5, 3.55, 0.5], head: [1.55, 1.2, 0.4] },
    build() {
      const g = new THREE.Group();
      g.add(extrude(roundRect(0.78, 0.46, 0.2), 0.06, mat(0xfdfbf6, { roughness: 0.6 }), 0.025));
      const tail = new THREE.Shape(); tail.moveTo(-0.26, -0.18); tail.lineTo(-0.4, -0.38); tail.lineTo(-0.12, -0.2); tail.closePath();
      g.add(extrude(tail, 0.06, mat(0xfdfbf6, { roughness: 0.6 }), 0.02));
      const dots = [-0.18, 0, 0.18].map((x) => { const d = ball(0.05, glossy(C.mint), [x, 0, 0.065], [1, 1, 0.5]); g.add(d); return d; });
      g.userData.dots = dots;
      return g;
    },
    tick(g, t, c) { g.userData.dots.forEach((d, i) => { const k = c.still ? 0.5 : Math.max(0, Math.sin(t * 5 - i * 0.9)); d.position.y = 0.04 * k; d.scale.set(1, 1, 0.5).multiplyScalar(0.85 + 0.3 * k); }); },
  },
  bell: {
    mount: "hand", work: "ring", use: "wave", scale: 1.25, grips: [null, [0, 0.36, 0]], carry: [0, 0, 0], show: [0, 0, 0], raise: [0, 0, 0],
    build() {
      const g = new THREE.Group(), body = new THREE.Group(); g.add(body);
      body.add(cyl(0.03, 0.035, 0.2, mat(C.woodDark, { roughness: 0.5 }), [0, 0.32, 0]));
      body.add(ball(0.04, mat(C.woodDark), [0, 0.43, 0]));
      body.add(lathe([[0.04, 0.2], [0.09, 0.18], [0.12, 0.06], [0.15, -0.06], [0.21, -0.12], [0.2, -0.13], [0.0, -0.13]], metal(C.gold)));
      body.add(ball(0.045, metal(0x9a7a3c), [0, -0.16, 0]));
      // rings of sound spread from the bell's mouth, facing you, off to the open side
      const rings = [0, 1].map(() => { const r = mesh(new THREE.TorusGeometry(0.16, 0.009, 6, 40, Math.PI * 1.2), new THREE.MeshBasicMaterial({ color: C.mint, transparent: true, opacity: 0, toneMapped: false, depthWrite: false })); r.castShadow = false; r.userData.fx = true; r.position.set(0.05, -0.08, 0); r.rotation.z = -Math.PI * 0.6; g.add(r); return r; });
      g.userData = { body, rings };
      return g;
    },
    tick(g, t, c) {
      const { body, rings } = g.userData, ringing = !c.still && c.ringing;
      body.rotation.z = ringing ? 0.35 * Math.sin(t * 16) * Math.max(0, Math.sin(t * 2.2)) : 0;
      rings.forEach((r, i) => { const ph = (t * 0.9 + i * 0.5) % 1; r.scale.setScalar(1 + ph * 1.1); r.material.opacity = ringing ? (1 - ph) * 0.7 : 0; });
    },
  },
  gift: {
    mount: "both", work: "show", use: "offer", scale: 1.15, grips: [[-0.25, -0.04, 0], [0.25, -0.04, 0]],
    offer: [0.12, 0.18, 0], hug: [0.06, 0.12, 0], offerY: 1.46, offerZ: 1.02, hugZ: 1.0,
    build() {
      const ribbon = glossy(C.mint);
      const g = group(rbox(0.44, 0.4, 0.44, 0.04, glossy(C.rose)), rbox(0.47, 0.1, 0.47, 0.03, glossy(C.rose), [0, 0.17, 0]));
      g.add(rbox(0.08, 0.43, 0.455, 0.01, ribbon, [0, 0.0, 0]), rbox(0.455, 0.43, 0.08, 0.01, ribbon, [0, 0.0, 0]));
      for (const sx of [-1, 1]) g.add(torus(0.08, 0.026, ribbon, [sx * 0.08, 0.27, 0], [0, 0, sx * 0.6]));
      g.add(ball(0.04, ribbon, [0, 0.24, 0]));
      return g;
    },
  },
  balloon: {
    mount: "hand", work: "show", use: "float", carryOnly: true, grips: [null, [0, 0, 0]], carry: [0, 0, -0.5], show: [0, 0, -0.5], raise: [0, 0, -0.2],
    build() {
      const g = new THREE.Group(), sway = new THREE.Group(); g.add(sway);
      const rubber = mat(C.coral, { roughness: 0.25, clearcoat: 1, clearcoatRoughness: 0.1 });
      sway.add(ball(0.27, rubber, [0, 1.48, 0], [0.95, 1.15, 0.95], 32));
      sway.add(mesh(new THREE.ConeGeometry(0.045, 0.07, 12), rubber, [0, 1.15, 0]));
      sway.add(tube([[0, 1.14, 0], [0.04, 0.8, 0.02], [-0.03, 0.4, -0.01], [0, 0.0, 0]], 0.007, mat(0xfdfbf5), 30));
      g.userData.sway = sway;
      return g;
    },
    tick(g, t, c) { const s = g.userData.sway; s.rotation.z = c.still ? 0 : 0.08 * Math.sin(t * 1.1); s.rotation.x = c.still ? 0 : 0.06 * Math.sin(t * 0.8 + 1); },
  },
  trophy: {
    mount: "both", work: "show", use: "celebrate", grips: [[-0.22, -0.2, 0], [0.22, -0.2, 0]],
    offer: [0.08, 0, 0], hug: [0.05, 0, 0], read: [-0.1, 0, 0], offerY: 1.5, hugY: 1.32, hugZ: 1.0,
    build() {
      const gold = metal(C.gold);
      const g = new THREE.Group();
      g.add(lathe([[0, -0.02], [0.2, 0.0], [0.23, 0.16], [0.2, 0.28], [0.22, 0.3], [0.0, 0.3]], gold));
      g.add(cyl(0.04, 0.06, 0.16, gold, [0, -0.1, 0]));
      g.add(rbox(0.3, 0.12, 0.22, 0.025, glossy(C.charcoal), [0, -0.24, 0]));
      g.add(plane(0.16, 0.05, glow(C.mint, 0.9), [0, -0.24, 0.111]));
      for (const sx of [-1, 1]) g.add(torus(0.08, 0.022, gold, [sx * 0.24, 0.16, 0], [0, 0, sx * 0.0], Math.PI * 1.2));
      g.children[4].rotation.z = Math.PI * 0.4; g.children[5].rotation.z = -Math.PI * 0.4 - Math.PI * 0.2 + Math.PI;
      g.add(extrude(star(0.07, 0.032), 0.02, glow(0xfff3c4, 1), 0.006, [0, 0.12, 0.22]));
      return g;
    },
  },
  heart: {
    mount: "both", work: "hug", use: "reassure", grips: [[-0.3, -0.04, -0.03], [0.3, -0.04, -0.03]],
    hug: [0.05, 0, 0], hugY: 1.33, hugZ: 1.0, offer: [0.1, 0, 0], offerY: 1.48,
    build() {
      const m = new THREE.MeshPhysicalMaterial({ color: C.rose, emissive: C.rose, emissiveIntensity: 0.18, roughness: 0.3, clearcoat: 1, clearcoatRoughness: 0.1 });
      const h = extrude(heartShape(0.17), 0.12, m, 0.07, [0, -0.02, 0]);
      const g = group(h); g.userData = { h, m };
      return g;
    },
    tick(g, t, c) {
      const ph = c.still ? 0.5 : (t * 1.15) % 1, beat = Math.max(Math.exp(-((ph - 0.05) ** 2) * 400), 0.7 * Math.exp(-((ph - 0.22) ** 2) * 400));
      g.userData.h.scale.setScalar(1 + 0.07 * beat); g.userData.m.emissiveIntensity = 0.18 + 0.25 * beat;
    },
  },
  shield: {
    mount: "hand", hand: 0, work: "show", use: "reassure", scale: 1.15, grips: [[0.02, -0.02, -0.1], null], carry: [0.05, -0.3, 0.05], show: [0.1, -0.15, 0.05], raise: [0, -0.2, 0], carryAt: [-0.82, 1.32, 0.6],
    build() {
      const s = new THREE.Shape(); s.moveTo(0, -0.36); s.bezierCurveTo(0.2, -0.24, 0.3, -0.06, 0.3, 0.28); s.lineTo(-0.3, 0.28); s.bezierCurveTo(-0.3, -0.06, -0.2, -0.24, 0, -0.36);
      const g = group(extrude(s, 0.05, glossy(C.teal), 0.025));
      const rim = new THREE.Shape(); rim.moveTo(0, -0.4); rim.bezierCurveTo(0.23, -0.27, 0.34, -0.07, 0.34, 0.31); rim.lineTo(-0.34, 0.31); rim.bezierCurveTo(-0.34, -0.07, -0.23, -0.27, 0, -0.4);
      g.add(extrude(rim, 0.03, metal(C.gold), 0.012, [0, 0, -0.03]));
      const mark = brandMark(0.13, glossy(C.mint)); mark.position.set(0, 0.0, 0.055); g.add(mark);
      return g;
    },
  },
};

for (const [id, spec] of Object.entries(PROPS)) spec.id = id;

// The companion an item brings for an action: a pencil to write in the notebook, a palette
// for the brush, a mug beside the teapot and for any tea break.
export function companionFor(action, id) {
  const spec = PROPS[id];
  if (!spec) return action === "sip" ? "mug" : "";
  if ((id === "notebook" && (action === "focus" || action === "write")) || (id === "pencil" && (action === "focus" || action === "write"))) return id === "notebook" ? "pencil" : "notebook";
  if (spec.companion === "brush" || spec.companion === "palette") return spec.companion;
  if (spec.companion) return spec.companion;
  if (action === "sip" && id !== "mug" && spec.mount !== "both") return "mug";
  return "";
}

export function makeProp(id, variant) {
  const spec = PROPS[id];
  if (!spec) return null;
  const root = spec.build(variant);
  root.name = `prop:${id}`;
  root.traverse((m) => { if (m.isMesh) { m.castShadow = m.castShadow !== false; m.receiveShadow = true; } });
  // how far the item reaches from its own origin (scaled), so a raise or a carry can keep its
  // bulk clear of the head whatever its shape
  if (!spec.extent) {
    const box = new THREE.Box3().setFromObject(root), k = spec.scale || 1;
    spec.extent = [Math.max(-box.min.x, box.max.x) * k, Math.max(-box.min.y, box.max.y) * k, Math.max(-box.min.z, box.max.z) * k];
    spec.top = box.max.y * k;
  }
  return { root, spec };
}

// Releases what a build allocated: its geometry and the materials made for it alone.
export function disposeProp(root) {
  const keep = new Set(shared.values());
  root.traverse((m) => {
    if (m.geometry) m.geometry.dispose();
    if (m.material && !keep.has(m.material) && !m.material.map) m.material.dispose();
  });
}
