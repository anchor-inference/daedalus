// The face the studio animates: the brand head from chibi2.js (a dark rounded screen with two eyes
// and a mint mouth) given eyelids, brows, a mouth that bends and opens, cheeks and small emotes.
// Every expression is a set of numbers and the face eases toward the set of the current mood, so
// a change of emotion moves the real 3D face instead of swapping one sticker for another. The
// first version only squashed the eyeballs and toggled mouth meshes, which read as one face.
import { THREE } from "../desktop/ui/assets/setup/stage3d.js";

const MINT = 0x7af3d6;
const CORAL = 0xff8f86;

// chibi2.js builds the head as roundedBlock(1, 0.86, 0.84, 0.42): its surface is the
// superellipsoid |x|^P + |y / 0.86|^P + |z / 0.84|^P = 1. Lines drawn on the face sit on it.
const P = 2 / 0.42;
export function faceZ(x, y) {
  const s = 1 - Math.pow(Math.min(1, Math.abs(x)), P) - Math.pow(Math.min(1, Math.abs(y / 0.86)), P);
  return 0.84 * Math.pow(Math.max(0, s), 1 / P);
}
export function insideHead(x, y, z, margin = 0) {
  return Math.pow(Math.abs(x) / (1 + margin), P) + Math.pow(Math.abs(y) / (0.86 + margin), P) + Math.pow(Math.abs(z) / (0.84 + margin), P) < 1;
}

const lineMaterial = (color, opacity = 1) => new THREE.MeshBasicMaterial({ color, toneMapped: false, transparent: opacity < 1, opacity, depthWrite: opacity >= 1 });
const capGeo = new THREE.CircleGeometry(1, 16);

// A flat stroke lying on the face, the way the brand draws its mouth: two vertices per point and
// round caps, rewritten in place every frame, so an expression change is a bend, not a swap.
class Stroke {
  constructor(n, material, parent) {
    this.n = n;
    this.pos = new Float32Array(n * 6);
    const geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(this.pos, 3).setUsage(THREE.DynamicDrawUsage));
    const index = [];
    for (let i = 0; i < n - 1; i++) { const a = i * 2; index.push(a, a + 1, a + 2, a + 1, a + 3, a + 2); }
    geo.setIndex(index);
    this.mesh = new THREE.Mesh(geo, material);
    this.mesh.frustumCulled = false;
    this.caps = [0, 1].map(() => { const c = new THREE.Mesh(capGeo, material); c.frustumCulled = false; parent.add(c); return c; });
    parent.add(this.mesh);
  }
  // pts: [x0, y0, x1, y1, ...] in head units; width: half width, a number or a function of 0..1
  set(pts, width, lift = 0.012) {
    const n = this.n, p = this.pos;
    for (let i = 0; i < n; i++) {
      const x = pts[i * 2], y = pts[i * 2 + 1];
      const a = Math.max(0, i - 1), b = Math.min(n - 1, i + 1);
      let tx = pts[b * 2] - pts[a * 2], ty = pts[b * 2 + 1] - pts[a * 2 + 1];
      const l = Math.hypot(tx, ty) || 1; tx /= l; ty /= l;
      const w = typeof width === "function" ? width(i / (n - 1)) : width;
      const nx = -ty * w, ny = tx * w, o = i * 6;
      p[o] = x + nx; p[o + 1] = y + ny; p[o + 2] = faceZ(x + nx, y + ny) + lift;
      p[o + 3] = x - nx; p[o + 4] = y - ny; p[o + 5] = faceZ(x - nx, y - ny) + lift;
    }
    this.mesh.geometry.attributes.position.needsUpdate = true;
    [0, n - 1].forEach((i, k) => {
      const c = this.caps[k], x = pts[i * 2], y = pts[i * 2 + 1];
      const w = typeof width === "function" ? width(i / (n - 1)) : width;
      c.position.set(x, y, faceZ(x, y) + lift);
      c.scale.setScalar(Math.max(w, 1e-4));
    });
  }
  set visible(v) { this.mesh.visible = v; this.caps.forEach((c) => { c.visible = v; }); }
}

// Eyelids. The white, the pupil and the glints of one eye share a lid shape: a fragment above
// the upper lid curve or below the lower one is cut away, so the dark face shows through as
// the lid. Curves are quadratics in the eye's own normalised coordinates (x, y in -1..1), which
// gives slanted lids for anger and sadness and a rising lower lid for smiling eyes. The cut is
// soft over a pixel and turned into coverage, so the edge stays antialiased under MSAA.
function lidMaterial(base, uniforms) {
  const m = base.clone();
  m.alphaToCoverage = true;
  m.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, uniforms);
    shader.vertexShader = shader.vertexShader
      .replace("#include <common>", "#include <common>\nuniform mat4 uEyeInv;\nvarying vec3 vEyeP;")
      .replace("#include <project_vertex>", "#include <project_vertex>\nvEyeP = ( uEyeInv * modelMatrix * vec4( transformed, 1.0 ) ).xyz;");
    shader.fragmentShader = shader.fragmentShader
      .replace("#include <common>", "#include <common>\nuniform vec3 uLidU;\nuniform vec3 uLidL;\nuniform float uLash;\nvarying vec3 vEyeP;")
      .replace("#include <alphatest_fragment>", `#include <alphatest_fragment>
      {
        float ex = vEyeP.x / 0.2;
        float ey = vEyeP.y / 0.25;
        float up = uLidU.x + uLidU.y * ex + uLidU.z * ex * ex;
        float lo = uLidL.x + uLidL.y * ex + uLidL.z * ex * ex;
        float d = min( up - ey, ey - lo );
        float aa = max( fwidth( d ), 1e-4 );
        float cover = smoothstep( -aa, aa, d );
        if ( cover <= 0.0 ) discard;
        diffuseColor.a *= cover;
        diffuseColor.rgb *= mix( uLash, 1.0, smoothstep( 0.0, 0.3, up - ey ) );
      }`);
  };
  return m;
}

// The numbers of each mood. Eyes: lidU / lidL are the upper and lower lid heights at the centre
// of the eye (1 is the top of the white, -1 the bottom); uTilt > 0 lowers the inner end of the
// upper lid (anger, effort), < 0 the outer end (sadness); uCurve sags the lid in the middle;
// lArch raises the middle of the lower lid (smiling eyes). Brows: browTilt > 0 lifts the inner
// ends (worry), < 0 pulls them down (resolve). Mouth: smile bends the line, open parts it,
// round makes the opening an O, asym lifts one corner, wave makes it wobble.
const NEUTRAL = {
  lidU: 1.3, uTilt: 0, uCurve: 0, lidL: -1.3, lArch: 0, eye: 1, pupil: 1, gx: 0, gy: 0,
  browY: 0, browTilt: 0, browArch: 0.5, browAsym: 0,
  smile: 0.4, open: 0, round: 0, mouthW: 0.15, asym: 0, wave: 0, tongue: 0,
  blush: 0.1, hatch: 0,
};
export const EXPRESSIONS = {
  calm: { lidU: 0.8, uCurve: 0.08, lidL: -0.84, lArch: 0.18, browTilt: 0.05, browArch: 0.6, smile: 0.55, mouthW: 0.15, blush: 0.14, emote: "" },
  joy: { lidU: 1.3, lidL: -0.62, lArch: 0.95, pupil: 1.05, browY: 0.07, browTilt: 0.1, browArch: 1, smile: 1, open: 0.62, round: 0.12, mouthW: 0.22, tongue: 0.8, blush: 0.55, emote: "sparkle" },
  curious: { lidU: 1.3, lidL: -1.2, eye: 1.07, pupil: 1.1, gx: 0.5, gy: 0.32, browY: 0.04, browAsym: 1, browArch: 0.8, smile: 0.1, open: 0.2, round: 1, mouthW: 0.06, blush: 0.12, emote: "question" },
  focused: { lidU: 0.46, uTilt: 0.12, lidL: -0.66, lArch: 0.08, pupil: 0.95, gy: -0.45, browY: -0.045, browTilt: -0.24, browArch: 0.12, smile: 0.06, mouthW: 0.085, blush: 0.04, emote: "" },
  worried: { lidU: 0.86, uTilt: -0.24, lidL: -0.98, pupil: 1.12, gy: -0.12, browY: 0.05, browTilt: 0.62, browArch: 0.2, smile: -0.42, open: 0.05, mouthW: 0.12, wave: 0.7, blush: 0.06, emote: "sweat" },
  sleepy: { lidU: 0.02, uCurve: 0.16, lidL: -0.7, lArch: 0.06, gy: -0.3, browY: -0.03, browTilt: 0.16, browArch: 0.35, smile: 0.12, mouthW: 0.08, blush: 0.16, emote: "zzz" },
  surprised: { lidU: 1.35, lidL: -1.35, eye: 1.17, pupil: 0.7, browY: 0.13, browArch: 1, smile: 0, open: 0.78, round: 1, mouthW: 0.085, blush: 0.12, emote: "exclaim" },
  proud: { lidU: 0.5, uCurve: -0.08, lidL: -0.6, lArch: 0.38, gx: 0.12, browY: 0.05, browTilt: -0.08, browArch: 0.55, browAsym: 0.3, smile: 0.78, asym: 0.55, mouthW: 0.15, blush: 0.28, emote: "sparkle" },
  shy: { lidU: 0.6, uCurve: 0.05, lidL: -0.7, lArch: 0.34, gx: -0.6, gy: -0.45, browY: 0.02, browTilt: 0.34, browArch: 0.4, smile: 0.25, mouthW: 0.07, wave: 1, blush: 0.95, hatch: 1, emote: "" },
  sad: { lidU: 0.36, uTilt: -0.34, uCurve: 0.05, lidL: -0.9, pupil: 1.14, gy: -0.55, browY: 0.02, browTilt: 0.78, browArch: 0.1, smile: -0.75, mouthW: 0.13, blush: 0.05, emote: "tear" },
  determined: { lidU: 0.54, uTilt: 0.4, lidL: -0.76, lArch: 0.12, pupil: 0.95, browY: -0.07, browTilt: -0.62, browArch: 0.05, smile: -0.1, mouthW: 0.13, asym: 0.12, blush: 0.06, emote: "" },
  affectionate: { lidU: 0.84, uTilt: -0.12, lidL: -0.5, lArch: 0.66, pupil: 1.16, browY: 0.03, browTilt: 0.26, browArch: 0.7, smile: 0.8, open: 0.08, mouthW: 0.17, blush: 0.8, emote: "heart" },
};
const KEYS = Object.keys(NEUTRAL);
export const expressionOf = (id) => ({ ...NEUTRAL, ...(EXPRESSIONS[id] || EXPRESSIONS.calm) });

// The curves of the face in head units. The 3D face and the little faces on the library cards
// both draw from these, so a card shows the expression the mascot will make.
export const browX = (side, u) => side * (0.22 + 0.32 * u);
export function browY(c, side, u) {
  const lift = c.browY + c.browAsym * (side > 0 ? 0.075 : -0.025);
  return 0.42 + lift + c.browTilt * (0.5 - u) * 0.15 + c.browArch * 0.05 * (1 - (2 * u - 1) ** 2) + (c.eye - 1) * 0.45;
}
// upper and lower lid heights across the eye (ex in -1..1, in the eye's own units)
export const lidUp = (up, c, inward, ex) => up - c.uTilt * inward * ex + c.uCurve * ex * ex;
export const lidLow = (lo, c, ex) => lo + c.lArch * (1 - ex * ex);
// a point across the mouth (s in -1..1): [x, upper lip y, lower lip y]
export function mouthAt(s, m) {
  const base = -m.smile * 0.075 * (1 - s * s) + m.asym * 0.045 * s + m.wave * 0.017 * Math.sin(s * Math.PI * 2.2) * (1 - s * s * 0.4);
  const prof = (1 - m.round) * (1 - s * s) + m.round * Math.sqrt(Math.max(0, 1 - s * s));
  const h = m.open * 0.19 * prof, upK = 0.12 + 0.38 * m.round;
  return [s * m.w, m.cy + base + h * upK, m.cy + base - h * (1 - upK)];
}
const EYE = { x: 0.38, y: 0.03, rx: 0.2, ry: 0.25 };
// A flat drawing of a mood's face: eye outlines cut by the lids (or the line of a closed eye),
// pupils, brows, the mouth, and how much the cheeks blush.
export function faceSketch(id) {
  const c = expressionOf(id);
  const eyes = [-1, 1].map((side) => {
    const inward = -side, sx = EYE.rx * c.eye, sy = EYE.ry * c.eye, top = [], bottom = [];
    for (let i = 0; i <= 24; i++) {
      const ex = -1 + (2 * i) / 24, edge = Math.sqrt(Math.max(0, 1 - ex * ex));
      const hi = Math.min(edge, lidUp(c.lidU, c, inward, ex)), lo = Math.max(-edge, lidLow(c.lidL, c, ex));
      if (hi > lo) { top.push([side * EYE.x + ex * sx, EYE.y + hi * sy]); bottom.unshift([side * EYE.x + ex * sx, EYE.y + lo * sy]); }
    }
    const open = top.length > 2;
    const meet = [];
    if (!open) for (let i = 0; i <= 12; i++) { const ex = -0.92 + 1.84 * i / 12; meet.push([side * EYE.x + ex * sx, EYE.y + Math.max(-1, Math.min(1, (lidUp(c.lidU, c, inward, ex) + lidLow(c.lidL, c, ex)) / 2)) * sy]); }
    return { white: open ? top.concat(bottom) : null, pupil: [side * EYE.x + c.gx * 0.072, EYE.y + c.gy * 0.085, 0.11 * c.pupil * c.eye], closed: open ? null : meet };
  });
  const brows = [-1, 1].map((side) => Array.from({ length: 11 }, (_, j) => [browX(side, j / 10), browY(c, side, j / 10)]));
  const m = { smile: c.smile, open: c.open, round: c.round, w: c.mouthW, asym: c.asym, wave: c.wave, cy: -0.31 };
  const upper = [], lower = [];
  for (let j = 0; j <= 18; j++) { const [x, u, l] = mouthAt(-1 + (2 * j) / 18, m); upper.push([x, u]); lower.push([x, l]); }
  return { eyes, brows, mouth: { upper, lower, open: c.open > 0.015 }, blush: c.blush, hatch: c.hatch };
}

// A gradient disc for the cheeks: a soft glow rather than a flat sticker.
const cheekTexture = (() => {
  const c = document.createElement("canvas"); c.width = c.height = 64;
  const g = c.getContext("2d"); const r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
  r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.45, "rgba(255,255,255,.55)"); r.addColorStop(1, "rgba(255,255,255,0)");
  g.fillStyle = r; g.fillRect(0, 0, 64, 64);
  return new THREE.CanvasTexture(c);
})();

// ---- emotes: the small signs cartoon faces use (z's, a sweat drop, a tear, sparkles) ----
function glyph(shape, color, depth = 0) {
  const geo = depth ? new THREE.ExtrudeGeometry(shape, { depth, bevelEnabled: true, bevelSize: depth * 0.4, bevelThickness: depth * 0.4, bevelSegments: 2, curveSegments: 10 }) : new THREE.ShapeGeometry(shape, 12);
  geo.center();
  return new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ color, toneMapped: false, transparent: true, opacity: 0, depthWrite: false }));
}
function zShape(s) {
  const z = new THREE.Shape(), w = s, h = s, t = s * 0.24;
  z.moveTo(-w / 2, h / 2); z.lineTo(w / 2, h / 2); z.lineTo(w / 2, h / 2 - t); z.lineTo(-w / 2 + t * 1.3, -h / 2 + t);
  z.lineTo(w / 2, -h / 2 + t); z.lineTo(w / 2, -h / 2); z.lineTo(-w / 2, -h / 2); z.lineTo(-w / 2, -h / 2 + t);
  z.lineTo(w / 2 - t * 1.3, h / 2 - t); z.lineTo(-w / 2, h / 2 - t); z.closePath();
  return z;
}
function sparkShape(r) {
  const s = new THREE.Shape();
  for (let i = 0; i < 8; i++) {
    const a = i * Math.PI / 4 + Math.PI / 2, k = i % 2 ? r * 0.26 : r;
    const x = Math.cos(a) * k, y = Math.sin(a) * k;
    if (!i) s.moveTo(x, y); else s.quadraticCurveTo(0, 0, x, y);
  }
  s.closePath();
  return s;
}
function heartShape(r) {
  const s = new THREE.Shape(); s.moveTo(0, -r);
  s.bezierCurveTo(-r * 2.1, r * 0.1, -r, r * 1.5, 0, r * 0.55);
  s.bezierCurveTo(r, r * 1.5, r * 2.1, r * 0.1, 0, -r);
  return s;
}
function dropMesh(r, color) {
  // a teardrop: a round bottom drawn up into a point
  const pts = [];
  for (let i = 0; i <= 10; i++) { const a = -Math.PI / 2 + (i / 10) * (Math.PI / 2); pts.push(new THREE.Vector2(Math.cos(a) * r, Math.sin(a) * r)); }
  for (let i = 1; i <= 10; i++) { const k = i / 10; pts.push(new THREE.Vector2(r * Math.pow(1 - k, 1.25), k * 1.9 * r)); }
  return new THREE.Mesh(new THREE.LatheGeometry(pts, 18), new THREE.MeshPhysicalMaterial({ color, roughness: 0.1, clearcoat: 1, transparent: true, opacity: 0, emissive: color, emissiveIntensity: 0.35, depthWrite: false }));
}
function markMesh(kind, color) {
  const g = new THREE.Group();
  const m = new THREE.MeshBasicMaterial({ color, toneMapped: false, transparent: true, opacity: 0, depthWrite: false });
  if (kind === "!") {
    const bar = new THREE.Mesh(new THREE.CapsuleGeometry(0.045, 0.2, 6, 12), m); bar.position.y = 0.09; bar.scale.x = 1.2; g.add(bar);
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.05, 12, 10), m); dot.position.y = -0.14; g.add(dot);
  } else {
    const curve = new THREE.CatmullRomCurve3([[-0.1, 0.12], [-0.06, 0.2], [0.04, 0.21], [0.1, 0.13], [0.06, 0.04], [0, -0.01], [0, -0.07]].map(([x, y]) => new THREE.Vector3(x, y, 0)));
    g.add(new THREE.Mesh(new THREE.TubeGeometry(curve, 30, 0.036, 8, false), m));
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.045, 12, 10), m); dot.position.y = -0.17; g.add(dot);
  }
  g.userData.material = m;
  return g;
}

class Emotes {
  constructor(head) {
    this.root = new THREE.Group(); head.add(this.root);
    this.w = { zzz: 0, sweat: 0, sparkle: 0, heart: 0, question: 0, exclaim: 0, tear: 0, note: 0 };
    this.zs = [0, 1, 2].map(() => { const m = glyph(zShape(0.2), MINT); this.root.add(m); return m; });
    this.sweat = dropMesh(0.075, 0xa9dcff); this.root.add(this.sweat);
    this.tear = dropMesh(0.05, 0xa9dcff); this.root.add(this.tear);
    this.sparks = [[0.98, 0.98, 0.25, 0.16, 0xffe08a], [-1.08, 0.72, 0.2, 0.12, MINT], [1.18, 0.32, 0.15, 0.1, 0xffe08a]].map(([x, y, z, r, c]) => { const m = glyph(sparkShape(r), c); m.position.set(x, y, z); m.userData.base = m.position.clone(); this.root.add(m); return m; });
    this.heart = glyph(heartShape(0.09), CORAL, 0.05); this.root.add(this.heart);
    this.question = markMesh("?", MINT); this.question.position.set(1.12, 1.12, 0.2); this.root.add(this.question);
    this.exclaim = markMesh("!", 0xffd27a); this.exclaim.position.set(1.08, 1.12, 0.2); this.root.add(this.exclaim);
  }
  update(dt, t, want, still) {
    const k = still ? 1 : 1 - Math.exp(-dt * 4);
    for (const key in this.w) this.w[key] += ((want === key ? 1 : 0) - this.w[key]) * k;
    const w = this.w, T = still ? 0.9 : t;
    // z's rise from the top corner one after another, growing and fading
    this.zs.forEach((m, i) => {
      const ph = still ? [0.25, 0.5, 0.75][i] : ((T / 3.3 + i / 3) % 1);
      m.position.set(0.82 + ph * 0.45, 0.82 + ph * 0.75, 0.25);
      m.rotation.z = 0.25 - ph * 0.3;
      m.scale.setScalar(0.65 + ph * 0.7);
      m.material.opacity = w.zzz * Math.sin(Math.min(1, ph * 1.15) * Math.PI) * 0.95;
      m.visible = m.material.opacity > 0.01;
    });
    // a sweat drop slides down the side of the head and fades
    {
      const ph = still ? 0.3 : (T / 2.8) % 1;
      this.sweat.position.set(0.97, 0.55 - ph * 0.3, 0.42);
      this.sweat.rotation.z = -0.25;
      this.sweat.material.opacity = w.sweat * (ph < 0.15 ? ph / 0.15 : 1 - Math.max(0, ph - 0.7) / 0.3) * 0.9;
      this.sweat.visible = this.sweat.material.opacity > 0.01;
    }
    // a tear wells at the outer corner of one eye and rolls down the cheek
    {
      const ph = still ? 0.35 : (T / 3.6) % 1;
      const y = -0.2 - Math.max(0, ph - 0.3) * 0.5, x = -0.6 - Math.max(0, ph - 0.3) * 0.04;
      this.tear.position.set(x, y, faceZ(x, y) + 0.05);
      this.tear.scale.setScalar(0.55 + Math.min(1, ph / 0.3) * 0.45);
      this.tear.material.opacity = w.tear * Math.min(1, ph / 0.2) * (1 - Math.max(0, ph - 0.75) / 0.25) * 0.9;
      this.tear.visible = this.tear.material.opacity > 0.01;
    }
    this.sparks.forEach((m, i) => {
      const ph = still ? 0.5 : ((T / 1.9 + i * 0.37) % 1);
      const s = Math.sin(ph * Math.PI);
      m.scale.setScalar(0.35 + s * 0.85);
      m.rotation.z = ph * 0.8;
      m.material.opacity = w.sparkle * s;
      m.visible = m.material.opacity > 0.01;
    });
    {
      const ph = still ? 0.45 : (T / 3) % 1;
      this.heart.position.set(0.95 + Math.sin(ph * 6) * 0.04, 0.95 + ph * 0.55, 0.25);
      this.heart.scale.setScalar(0.8 + ph * 0.6);
      this.heart.material.opacity = w.heart * Math.sin(Math.min(1, ph * 1.1) * Math.PI);
      this.heart.visible = this.heart.material.opacity > 0.01;
    }
    this.question.rotation.z = still ? 0.12 : 0.12 + Math.sin(T * 1.7) * 0.12;
    this.question.position.y = 1.12 + (still ? 0 : Math.sin(T * 2.1) * 0.03);
    this.question.userData.material.opacity = w.question;
    this.question.visible = w.question > 0.01;
    {
      const pulse = still ? 1 : 1 + 0.07 * Math.sin(T * 3.2);
      this.exclaim.scale.setScalar(Math.max(0.01, w.exclaim) * pulse);
      this.exclaim.rotation.z = still ? -0.12 : -0.12 + Math.sin(T * 1.9) * 0.06;
      this.exclaim.userData.material.opacity = w.exclaim;
      this.exclaim.visible = w.exclaim > 0.01;
    }
  }
}

export class FaceRig {
  constructor(model) {
    this.model = model;
    const head = model.head;
    this.head = head;
    model.face.mouth.visible = false;
    // the brand's faint mint cheeks: replaced by cheeks that can also blush
    head.children.forEach((c) => { if (c.isMesh && c.geometry.type === "CircleGeometry") c.visible = false; });
    this.lineMat = lineMaterial(MINT);
    this.eyes = model.face.eyes.map(({ e, pv }) => {
      const side = e.position.x < 0 ? -1 : 1;
      const u = { uEyeInv: { value: new THREE.Matrix4() }, uLidU: { value: new THREE.Vector3(1.3, 0, 0) }, uLidL: { value: new THREE.Vector3(-1.3, 0, 0) }, uLash: { value: 0.42 } };
      const white = e.children[0];
      white.material = lidMaterial(white.material, u);
      pv.children.forEach((m) => { m.material = lidMaterial(m.material, u); });
      const pupil = pv.children[0];
      const closed = new Stroke(13, this.lineMat, head);
      return { e, pv, side, u, white, pupil, pupilScale: pupil.scale.clone(), glints: pv.children.slice(1).map((g) => ({ g, s: g.scale.clone(), p: g.position.clone() })), closed, base: e.position.clone() };
    });
    this.brows = [-1, 1].map(() => new Stroke(11, this.lineMat, head));
    this.upperLip = new Stroke(19, this.lineMat, head);
    this.lowerLip = new Stroke(19, this.lineMat, head);
    {
      const n = 19;
      this.mouthPos = new Float32Array(n * 2 * 3);
      this.mouthCol = new Float32Array(n * 2 * 3);
      const geo = new THREE.BufferGeometry();
      geo.setAttribute("position", new THREE.BufferAttribute(this.mouthPos, 3).setUsage(THREE.DynamicDrawUsage));
      geo.setAttribute("color", new THREE.BufferAttribute(this.mouthCol, 3).setUsage(THREE.DynamicDrawUsage));
      const index = [];
      for (let i = 0; i < n - 1; i++) { const a = i * 2; index.push(a, a + 2, a + 1, a + 1, a + 2, a + 3); }
      geo.setIndex(index);
      this.mouthFill = new THREE.Mesh(geo, new THREE.MeshBasicMaterial({ vertexColors: true, toneMapped: false, side: THREE.DoubleSide }));
      this.mouthFill.frustumCulled = false;
      head.add(this.mouthFill);
    }
    this.cheeks = [-1, 1].map((side) => {
      const m = new THREE.Mesh(new THREE.PlaneGeometry(0.34, 0.26), new THREE.MeshBasicMaterial({ map: cheekTexture, color: CORAL, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false }));
      m.position.set(side * 0.62, -0.17, faceZ(0.62, -0.17) + 0.006); m.rotation.y = side * 0.2;
      head.add(m);
      return m;
    });
    this.hatchMat = lineMaterial(0xff9c94, 0.999);
    this.hatches = [];
    for (const side of [-1, 1]) for (let j = 0; j < 3; j++) {
      const s = new Stroke(2, this.hatchMat, head);
      const cx = side * 0.62 + (j - 1) * 0.07;
      s.set([cx - 0.025, -0.21, cx + 0.025, -0.13], 0.012, 0.016);
      this.hatches.push(s);
    }
    this.emotes = new Emotes(head);
    this.cur = { ...NEUTRAL, ...EXPRESSIONS.calm };
    this.emotion = "calm";
    this.blinkAt = 1.2; this.blinkLen = 0.16; this.blinkTwice = false;
    this.pts = new Float32Array(64);
    this.mouthCx = 0; this.mouthCy = -0.3;
    this.tmp = new THREE.Vector3();
  }

  // o: { emotion, still, close, open, smile, round, gazeX, gazeY, gazeK, look (head-local unit
  // vector to look along, or null), speak, magnify ([eye index, factor] or null), noBlink }
  update(dt, t, o) {
    const target = EXPRESSIONS[o.emotion] || EXPRESSIONS.calm;
    const c = this.cur;
    const k = o.still ? 1 : 1 - Math.exp(-dt * 8);
    for (const key of KEYS) c[key] += ((target[key] ?? NEUTRAL[key]) - c[key]) * k;

    // speech opens the mouth and rounds it a little, over whatever the mood shape is
    const speak = o.speak || 0;
    const open = Math.min(1, Math.max(0, c.open + (o.open || 0)) * (1 - speak * 0.4) + speak * 0.62);
    const round = Math.min(1, c.round + (o.round || 0) + speak * 0.35 * (1 - c.round));
    const smile = c.smile + (o.smile || 0) - speak * 0.15;
    const width = c.mouthW * (1 - 0.25 * round * open) + speak * 0.01;

    // blinking: a short close every few seconds, sometimes twice; not while asleep
    let blink = 0;
    if (!o.still && !o.noBlink) {
      if (t > this.blinkAt + this.blinkLen * (this.blinkTwice ? 2.4 : 1)) {
        const r = Math.abs(Math.sin(this.blinkAt * 12.9898) * 43758.5453) % 1;
        this.blinkTwice = r < 0.18;
        this.blinkAt = t + 2.2 + r * 3.4;
      }
      const dtb = t - this.blinkAt;
      const one = (x) => (x >= 0 && x <= this.blinkLen ? Math.sin((x / this.blinkLen) * Math.PI) : 0);
      blink = Math.max(one(dtb), this.blinkTwice ? one(dtb - this.blinkLen * 1.4) : 0);
    }
    const close = Math.min(1, Math.max(o.close || 0, 0));
    const meet = -0.22 + 0.38 * Math.min(1, Math.max(0, c.lArch));
    // where the eyes look: the mood's glance, replaced by the motion's (gazeK), then aimed at a
    // point (the viewer, a page, a pencil tip) when the motion gives one
    let gx = c.gx + ((o.gazeX || 0) - c.gx) * (o.gazeK || 0);
    let gy = c.gy + ((o.gazeY || 0) - c.gy) * (o.gazeK || 0);
    if (o.look) {
      const lk = o.lookK == null ? 1 : o.lookK;
      gx += (Math.max(-1, Math.min(1, o.look.x * 2.6)) - gx) * lk;
      gy += (Math.max(-1, Math.min(1, o.look.y * 2.6)) - gy) * lk;
    }
    gx = Math.max(-1, Math.min(1, gx)); gy = Math.max(-1, Math.min(1, gy));

    this.model.root.updateMatrixWorld(true);
    const lids = (1 - close) * (1 - blink);
    this.eyes.forEach((eye, i) => {
      const inward = -eye.side;
      const mag = o.magnify && o.magnify[0] === i ? o.magnify[1] : 1;
      eye.e.scale.setScalar(c.eye * mag);
      eye.e.position.copy(eye.base);
      const pupil = c.pupil * (1 + 0.06 * Math.sin(t * 0.7 + i) * (o.still ? 0 : 1));
      eye.pupil.scale.copy(eye.pupilScale).multiplyScalar(pupil);
      eye.glints.forEach(({ g, s }) => g.scale.copy(s).multiplyScalar(0.85 + 0.15 * pupil));
      eye.pv.position.set(gx * 0.072, gy * 0.085, 0);
      const up = meet + (c.lidU - meet) * lids;
      const lo = meet + (c.lidL - meet) * lids;
      eye.u.uLidU.value.set(up, -c.uTilt * inward, c.uCurve);
      eye.u.uLidL.value.set(lo + c.lArch, 0, -c.lArch);
      eye.e.updateMatrixWorld(true);
      eye.u.uEyeInv.value.copy(eye.e.matrixWorld).invert();
      // closed eyes are drawn as a mint arc where the lids meet, like the mouth line
      const gap = up - (lo + c.lArch);
      const shut = Math.max(0, Math.min(1, (0.32 - gap) / 0.26));
      eye.closed.visible = shut > 0.02;
      if (shut > 0.02) {
        const n = 13, p = this.pts, sx = 0.2 * c.eye * mag, sy = 0.25 * c.eye * mag;
        for (let j = 0; j < n; j++) {
          const ex = -0.92 + 1.84 * j / (n - 1);
          const yu = lidUp(up, c, inward, ex);
          const yl = lidLow(lo, c, ex);
          const ey = Math.max(-1, Math.min(1, (yu + yl) / 2));
          p[j * 2] = eye.base.x + ex * sx; p[j * 2 + 1] = eye.base.y + ey * sy;
        }
        eye.closed.set(p, (s) => 0.021 * shut * (0.55 + 0.45 * Math.sin(s * Math.PI)), 0.03);
      }
    });

    // brows: above the eyes, the inner end thicker, bent by the mood
    this.brows.forEach((b, i) => {
      const side = i ? 1 : -1, n = 11, p = this.pts;
      for (let j = 0; j < n; j++) { const u = j / (n - 1); p[j * 2] = browX(side, u); p[j * 2 + 1] = browY(c, side, u); }
      b.set(p, (u) => 0.026 * (1 - 0.38 * u));
    });

    // the mouth: an upper and a lower line that coincide when closed, a dark opening between
    {
      const n = 19, up = this.pts;
      const cy = -0.31, w = width;
      const pos = this.mouthPos, col = this.mouthCol;
      const lower = this.lowerPts || (this.lowerPts = new Float32Array(n * 2));
      const shape = this.mouthShape || (this.mouthShape = {});
      Object.assign(shape, { smile, open, round, w, asym: c.asym, wave: c.wave, cy });
      for (let j = 0; j < n; j++) {
        const s = -1 + 2 * j / (n - 1);
        const [x, yu, yl] = mouthAt(s, shape);
        up[j * 2] = x; up[j * 2 + 1] = yu;
        lower[j * 2] = x; lower[j * 2 + 1] = yl;
        const z0 = faceZ(x, cy) + 0.007;
        pos[j * 6] = x; pos[j * 6 + 1] = up[j * 2 + 1]; pos[j * 6 + 2] = z0;
        pos[j * 6 + 3] = x; pos[j * 6 + 4] = lower[j * 2 + 1]; pos[j * 6 + 5] = z0;
        const tg = c.tongue * Math.pow(Math.max(0, 1 - s * s), 1.5) * Math.min(1, open * 2.2);
        col[j * 6] = 0.03; col[j * 6 + 1] = 0.07; col[j * 6 + 2] = 0.075;
        col[j * 6 + 3] = 0.03 + tg * 0.9; col[j * 6 + 4] = 0.07 + tg * 0.36; col[j * 6 + 5] = 0.075 + tg * 0.33;
      }
      this.mouthFill.geometry.attributes.position.needsUpdate = true;
      this.mouthFill.geometry.attributes.color.needsUpdate = true;
      this.mouthFill.visible = open > 0.015;
      this.upperLip.set(up, 0.026);
      this.lowerLip.visible = open > 0.015;
      if (open > 0.015) this.lowerLip.set(lower, 0.026);
    }

    // cheeks: the brand's faint mint glow, warming to a coral blush
    const blush = Math.min(1, c.blush + (o.blush || 0));
    this.cheeks.forEach((m) => {
      m.material.color.setHex(MINT).lerp(this.tmpColor || (this.tmpColor = new THREE.Color(CORAL)), Math.min(1, blush * 2.2));
      // faint at rest (on the dark screen a strong neutral glow reads as a smudge), warm when it blushes
      m.material.opacity = 0.04 + blush * 0.66;
    });
    this.hatchMat.opacity = c.hatch * 0.95;
    this.hatches.forEach((h) => { h.visible = c.hatch > 0.03; });

    this.emotes.update(dt, t, o.emote != null ? o.emote : target.emote, o.still);
  }
}
