// How the mascots move. Each frame one pose is built from three layers: the idle life underneath
// (breathing, a slow weight shift, small glances), the action (a loop of deliberate key poses),
// and the mood's posture on top. A pose is plain numbers, so a new action is a blend from the
// pose the mascot is in, never a snap; props are placed by the same pose, and the hands that hold
// them are solved onto their grips, so a held thing never floats beside the hand.

export const TAU = Math.PI * 2;
export const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
export const lerp = (a, b, k) => a + (b - a) * k;
export const smooth = (x) => { x = clamp(x, 0, 1); return x * x * (3 - 2 * x); };
// 0 → 1 over [a, b] of x, eased at both ends
export const ramp = (x, a, b) => smooth((x - a) / (b - a));
// rises over [a, b], holds, falls over [c, d]
export const env = (x, a, b, c, d) => ramp(x, a, b) * (1 - ramp(x, c, d));
const hash = (n) => { const s = Math.sin(n * 127.1 + 311.7) * 43758.5453; return s - Math.floor(s); };
// smooth value noise in -1..1, for idle variety that never repeats exactly
export function noise(t, seed = 0) { const i = Math.floor(t), f = t - i; return lerp(hash(i + seed * 17.31), hash(i + 1 + seed * 17.31), f * f * (3 - 2 * f)) * 2 - 1; }

// chibi2.js makeChibi: the arms hang from these points of the body (design units) and the mitten
// sits 0.62 down the arm. There is no elbow, so an arm is one bone that turns and, like a
// rubber-hose cartoon arm, stretches a little to reach.
export const SHOULDER = [[-0.62, 1.5, 0.05], [0.62, 1.5, 0.05]];
export const ARM = 0.62;

// Rotates v by an XYZ Euler (three.js order: R = Rx · Ry · Rz).
export function rotate(v, rx, ry, rz, out = [0, 0, 0]) {
  let x = v[0], y = v[1], z = v[2], c, s, a;
  c = Math.cos(rz); s = Math.sin(rz); a = x * c - y * s; y = x * s + y * c; x = a;
  c = Math.cos(ry); s = Math.sin(ry); a = x * c + z * s; z = -x * s + z * c; x = a;
  c = Math.cos(rx); s = Math.sin(rx); a = y * c - z * s; z = y * s + z * c; y = a;
  out[0] = x; out[1] = y; out[2] = z;
  return out;
}

const BODY_KEYS = [
  "lift", "squash", "yaw", "lean", "roll", "bx", "by", "hx", "hy", "hz",
  "a0x", "a0z", "a0s", "a1x", "a1z", "a1s", "s0x", "s0y", "s0z", "s1x", "s1y", "s1z",
  "l0x", "l0z", "l1x", "l1z", "wing", "wing0", "wing1", "flap",
  "gx", "gy", "gk", "cam", "close", "open", "smile", "round", "speak", "blush",
  "p1x", "p1y", "p1z", "p1rx", "p1ry", "p1rz", "p2x", "p2y", "p2z", "p2rx", "p2ry", "p2rz", "g0", "g1",
];

export function neutral() {
  const P = {};
  for (const k of BODY_KEYS) P[k] = 0;
  P.squash = 1; P.a0z = -0.28; P.a1z = 0.28; P.a0s = 1; P.a1s = 1; P.cam = 0.85; P.flap = 0.12;
  P.G0 = 0; P.G1 = 0; P.emote = null; P.free0 = true; P.free1 = true; P.lookAt = null; P.magnify = null;
  P.m1 = false; P.m2 = false; P.lockHead = false;
  return P;
}
// a pose between two poses; the non-numeric choices come from the nearer one
export function blend(A, B, k, out = {}) {
  for (const key of BODY_KEYS) out[key] = A[key] + (B[key] - A[key]) * k;
  const near = k < 0.5 ? A : B;
  out.G0 = near.G0; out.G1 = near.G1; out.emote = near.emote; out.free0 = near.free0; out.free1 = near.free1;
  out.lookAt = near.lookAt; out.magnify = near.magnify; out.m1 = near.m1; out.m2 = near.m2;
  // a hand that holds an item in only one of the two poses lets go or takes hold over the blend
  if (A.G0 !== B.G0) out.g0 = (k < 0.5 ? A.g0 * (1 - 2 * k) : B.g0 * (2 * k - 1));
  if (A.G1 !== B.G1) out.g1 = (k < 0.5 ? A.g1 * (1 - 2 * k) : B.g1 * (2 * k - 1));
  return out;
}

// Solves arm i onto a point of the body's space, writing its angles and stretch. The angle about
// x is kept in (-3π/2, π/2] so an arm raised past vertical does not wrap around through the body.
export function reach(P, i, x, y, z, fixed) {
  const S = SHOULDER[i];
  const sx = S[0] + (i ? P.s1x : P.s0x), sy = S[1] + (i ? P.s1y : P.s0y), sz = S[2] + (i ? P.s1z : P.s0z);
  let dx = x - sx, dy = y - sy, dz = z - sz;
  const d = Math.hypot(dx, dy, dz) || 1e-6;
  dx /= d; dy /= d; dz /= d;
  const az = Math.asin(clamp(dx, -1, 1));
  let ax = Math.atan2(-dz, -dy);
  if (ax > Math.PI / 2) ax -= TAU;
  const s = fixed ? 1 : clamp(d / ARM, 0.82, 1.38);
  if (i) { P.a1x = ax; P.a1z = az; P.a1s = s; } else { P.a0x = ax; P.a0z = az; P.a0s = s; }
}

const v3 = [0, 0, 0];
function place(P, n, x, y, z, rx = 0, ry = 0, rz = 0) {
  if (n === 1) { P.p1x = x; P.p1y = y; P.p1z = z; P.p1rx = rx; P.p1ry = ry; P.p1rz = rz; }
  else { P.p2x = x; P.p2y = y; P.p2z = z; P.p2rx = rx; P.p2ry = ry; P.p2rz = rz; }
}
// The grip of an item for a hand, in the item's own space (scaled). One-handed items describe the
// right-of-screen hand (1); in the other hand the grip and the turn are mirrored.
export function gripOf(spec, hand) {
  const s = spec.scale || 1, g = spec.grips || [];
  const own = g[hand];
  if (own) return [own[0] * s, own[1] * s, own[2] * s];
  const other = g[1 - hand];
  if (other) return [-other[0] * s, other[1] * s, other[2] * s];
  return [0, 0, 0];
}
// The hand a one-handed item lives in (most in the hand on the right of the screen, a palette or
// a shield on the other arm), and so the hand left free for gestures.
export const itemHand = (spec) => (spec && spec.mount === "hand" ? (spec.hand == null ? 1 : spec.hand) : -1);
function hold(P, hand, n, k = 1) {
  if (hand) { P.g1 = k; P.G1 = n; P.free1 = false; } else { P.g0 = k; P.G0 = n; P.free0 = false; }
}
// The hand goes to (x, y, z); the item hangs from it at its grip, turned by (rx, ry, rz).
function holdOne(P, n, spec, hand, x, y, z, rx = 0, ry = 0, rz = 0) {
  const g = spec.grips || [];
  const mirror = !g[hand];
  if (mirror) { ry = -ry; rz = -rz; }
  if (n === 1) P.m1 = mirror && hand === 0; else P.m2 = mirror && hand === 0;
  rotate(gripOf(spec, hand), rx, ry, rz, v3);
  place(P, n, x - v3[0], y - v3[1], z - v3[2], rx, ry, rz);
  reach(P, hand, x, y, z);
  hold(P, hand, n);
}
// The item sits at (x, y, z) turned (rx, ry, rz) and both hands go to its grips.
function holdTwo(P, n, spec, x, y, z, rx = 0, ry = 0, rz = 0, hands = [0, 1]) {
  place(P, n, x, y, z, rx, ry, rz);
  for (const h of hands) {
    rotate(gripOf(spec, h), rx, ry, rz, v3);
    reach(P, h, x + v3[0], y + v3[1], z + v3[2]);
    hold(P, h, n);
  }
}
// Shoulders roll forward and in when both hands work in front of the round belly; without it
// the short arms could not reach past it.
function hunch(P, k = 1, i = 2) {
  if (i !== 1) { P.s0x += 0.08 * k; P.s0z += 0.22 * k; }
  if (i !== 0) { P.s1x -= 0.08 * k; P.s1z += 0.22 * k; }
}

// ---- the idle life every action stands on ----
function life(P, c) {
  const t = c.t, br = c.breath;
  P.by = 0.016 * br; P.squash = 1 + 0.008 * br;
  P.s0y = P.s1y = 0.014 * br;
  const w = Math.sin(t * TAU / 7.3);
  P.bx = 0.018 * w; P.roll = -0.012 * w;
  P.hy = 0.11 * noise(t * 0.21, 1) + 0.035 * noise(t * 0.63, 2);
  P.hx = 0.035 * noise(t * 0.29, 3) - 0.01 * br;
  P.hz = 0.01 * noise(t * 0.25, 4);
  P.a0x = 0.05 * noise(t * 0.37, 5); P.a1x = 0.05 * noise(t * 0.37, 6);
  P.a0z = -0.27 - 0.025 * br; P.a1z = 0.27 + 0.025 * br;
  P.flap = 0.12; P.cam = 0.85;
}

// A held item in the "carry" place: one-handed things low beside the hip, two-handed against the belly.
function carry(P, c, n, spec, hand) {
  if (!spec) return;
  if (hand == null) hand = spec.hand == null ? 1 : spec.hand;
  if (spec.mount === "hand") {
    const r = spec.carry || [0, 0, 0], at = spec.carryAt || [-0.84, 1.1, 0.55], sx = hand ? -1 : 1;
    const sway = 0.02 * Math.sin(c.t * TAU / 7.3);
    holdOne(P, n, spec, hand, at[0] * sx, at[1] + sway, at[2], r[0], r[1], r[2]);
  } else if (spec.mount === "both") {
    const r = spec.hug || [0.08, 0, 0];
    hunch(P);
    const z = spec.hugZ || 0.98;
    holdTwo(P, n, spec, 0, (spec.hugY || 1.28) + 0.01 * c.breath, z, r[0], r[1], r[2]);
  }
}
// A two-handed item held against the body by one arm, leaving the other free.
function tuck(P, c, n, spec, hand = 0) {
  if (!spec) return;
  if (spec.mount === "hand") { carry(P, c, n, spec); return; }
  if (spec.mount !== "both") return;
  const t = spec.tuck || [-0.58, 1.18, 0.78, 0.1, -0.75, 0.05];
  const sx = hand ? -1 : 1;
  if (hand) hunch(P, 0.6, 1); else hunch(P, 0.6, 0);
  place(P, n, t[0] * sx, t[1], t[2], t[3], t[4] * sx, t[5] * sx);
  rotate(gripOf(spec, hand), t[3], t[4] * sx, t[5] * sx, v3);
  reach(P, hand, t[0] * sx + v3[0], t[1] + v3[1], t[2] + v3[2]);
  hold(P, hand, n);
}

// The arms of a mood, for hands the action leaves free.
function moodArms(P, c, w) {
  const hand = (i, x, y, z, k) => {
    if (!(i ? P.free1 : P.free0) || k < 0.01) return;
    const Q = { ...P };
    if (i) hunch(Q, 1, 1); else hunch(Q, 1, 0);
    reach(Q, i, x, y, z);
    const a = i ? ["a1x", "a1z", "a1s", "s1x", "s1z"] : ["a0x", "a0z", "a0s", "s0x", "s0z"];
    for (const key of a) P[key] += (Q[key] - P[key]) * k;
  };
  const both = (x, y, z, k) => { hand(0, -x, y, z, k); hand(1, x, y, z, k); };
  const wig = c.still ? 0 : 1;
  both(0.17, 1.22 + 0.012 * Math.sin(c.t * 5) * wig, 0.9, w.worried);
  both(0.16, 1.07, 0.9, w.shy);
  both(0.16, 1.46 + 0.01 * c.breath, 0.87, w.affectionate);
  both(0.43, 1.62, 0.8, w.determined);
  both(0.8, 1.0, 0.02, w.proud);
  const open = w.joy * 0.45 + w.surprised * 0.75;
  if (P.free0) P.a0z -= open; if (P.free1) P.a1z += open;
  if (P.free0) { P.a0z += 0.12 * w.sad + 0.08 * w.sleepy; P.a0x += 0.05 * w.sad; }
  if (P.free1) { P.a1z -= 0.12 * w.sad + 0.08 * w.sleepy; P.a1x += 0.05 * w.sad; }
}

// What a mood does to the body whatever the action: where the head hangs, how the wings sit,
// a bounce of joy, the slump of sadness. Weighted by how far each mood has faded in.
export function moodPosture(P, c, w, armsAllowed) {
  const t = c.t, still = c.still ? 0 : 1;
  P.hx += 0.09 * w.focused + 0.08 * w.worried + 0.15 * w.sleepy + 0.14 * w.shy + 0.22 * w.sad + 0.03 * w.determined
    - 0.08 * w.surprised - 0.13 * w.proud - 0.03 * w.joy;
  P.hz += 0.13 * w.curious + 0.07 * w.sleepy + 0.1 * w.shy + 0.12 * w.affectionate;
  P.hy += 0.08 * w.curious - 0.14 * w.shy;
  P.lean += 0.06 * w.curious + 0.04 * w.worried + 0.06 * w.sad + 0.05 * w.determined - 0.08 * w.surprised - 0.07 * w.proud + 0.03 * w.sleepy;
  P.wing += 0.3 * w.joy + 0.15 * w.curious - 0.1 * w.focused - 0.15 * w.worried - 0.4 * w.sleepy + 0.6 * w.surprised
    + 0.3 * w.proud - 0.2 * w.shy - 0.5 * w.sad + 0.25 * w.determined + 0.1 * w.affectionate;
  P.flap += 0.3 * w.joy + 0.35 * w.surprised + 0.12 * w.affectionate - 0.08 * w.sad - 0.08 * w.sleepy;
  P.s0y += 0.05 * w.worried + 0.03 * w.shy - 0.03 * w.sad; P.s1y += 0.05 * w.worried + 0.03 * w.shy - 0.03 * w.sad;
  // joy bounces lightly on its toes; affection and shyness sway; sleep nods off and catches itself
  P.by += w.joy * 0.03 * Math.abs(Math.sin(t * Math.PI * 1.25)) * still;
  P.roll += (w.affectionate * 0.035 + w.shy * 0.025) * Math.sin(t * TAU / 3.4) * still;
  P.hz += w.joy * 0.03 * Math.sin(t * Math.PI * 1.25) * still;
  const drop = Math.max(0, Math.sin(t * TAU / 6.5)) ** 6;
  P.hx += w.sleepy * 0.12 * drop * still;
  // a mood that is a jolt (surprise) lifts the body for a moment when it arrives
  P.by += w.surprised * 0.03 * (1 - w.surprised) * 4;
  if (armsAllowed) moodArms(P, c, w);
}

// ---- actions ----
// Each takes the pose filled with the idle life and the context: t (clock), e (seconds since the
// action began, so a loop starts at its beginning), still (reduced motion: t and e are fixed key
// times), item / comp (the specs of the chosen prop and of a companion such as the pencil that
// goes with a notebook), voice. Gestures use the hand that holds nothing; a one-handed item
// stays in its own hand through every action, so it never jumps from hand to hand.
const A = {};
// the hand free for a gesture, and the screen side it is on
function freeHand(c) { const h = itemHand(c.item); return h === 1 ? 0 : 1; }
// keeps the chosen items where an action that does not use them would leave them
function keep(P, c, gestureHand) {
  const it = c.item;
  if (it) {
    if (it.mount === "both") { if (gestureHand == null) carry(P, c, 1, it); else tuck(P, c, 1, it, 1 - gestureHand); }
    else if (it.mount === "hand") carry(P, c, 1, it);
  }
  if (c.comp && c.comp.mount === "hand") carry(P, c, 2, c.comp);
}

A.idle = (P, c) => { keep(P, c); };

// Waves with the free hand: three easy waves, a beat with the hand up, again. With the bell in
// hand it rings the bell instead, up beside the head.
A.wave = (P, c) => {
  const it = c.item;
  if (it && it.work === "ring") {
    hunch(P, 0.5, 1);
    const shake = c.still ? 0 : Math.sin(c.e * TAU * 2.4) * 0.03 * Math.max(0, Math.sin(c.e * 2.2));
    holdOne(P, 1, it, 1, 1.04 + shake, 1.76, 0.5, 0, 0, 0);
    P.hz -= 0.06; P.hy += 0.08; P.cam = 1; P.smile += 0.2; P.wing1 += 0.2;
    return;
  }
  const hand = freeHand(c), sx = hand ? 1 : -1;
  keep(P, c, hand);
  const ph = (c.e % 2.6) / 2.6;
  const waving = c.still ? 0 : env(ph, 0, 0.08, 0.62, 0.72);
  const sw = Math.sin(c.e * TAU * 1.6) * 0.32 * waving;
  if (hand) { P.a1z = 2.3 + sw; P.a1x = 0.32; P.a1s = 1.04; P.free1 = false; }
  else { P.a0z = -(2.3 + sw); P.a0x = 0.32; P.a0s = 1.04; P.free0 = false; }
  P.hz += -sx * 0.07; P.hy += sx * 0.05; P.roll += -sx * 0.025 * Math.sin(c.e * TAU * 1.6) * waving;
  P[hand ? "wing1" : "wing0"] += 0.2; P.smile += 0.15; P.cam = 1;
};

// "I heard you": two soft nods, eyes easing shut on the way down, a pause.
A.nod = (P, c) => {
  keep(P, c);
  const ph = c.still ? 0.11 : (c.e % 2.2) / 2.2;
  const nod = Math.sin(clamp(ph / 0.22, 0, 1) * Math.PI) + 0.75 * Math.sin(clamp((ph - 0.26) / 0.22, 0, 1) * Math.PI);
  P.hx += 0.24 * nod; P.by -= 0.02 * nod; P.lean += 0.03 * nod;
  P.close = Math.max(P.close, 0.4 * nod); P.smile += 0.2; P.cam = 1;
};

// Leans out to one side as if from behind something, looks, tucks back in.
A.peek = (P, c) => {
  keep(P, c);
  const ph = c.still ? 0.5 : (c.e % 4.4) / 4.4;
  const out = env(ph, 0.05, 0.25, 0.72, 0.92);
  P.roll += -0.22 * out; P.bx += 0.16 * out; P.lean += 0.05 * out;
  P.hz += -0.12 * out; P.hy += 0.12 * out;
  P.l0z = -0.32 * out; P.l0x = -0.12 * out;
  if (P.free0) { P.a0z = lerp(P.a0z, -1.0, out); P.a0x = lerp(P.a0x, -0.25, out); }
  P.wing0 += 0.5 * out;
  P.cam = 1; P.open += 0.12 * out; P.round += 0.5 * out;
};

// Points with the free hand at what is there (the clock on the plinth, the paper plane, the
// calendar it holds up) or, with nothing about, to the side; looks there, then back to you.
A.point = (P, c) => {
  const it = c.item;
  const ph = c.still ? 0.3 : (c.e % 4.2) / 4.2;
  const tap = c.still ? 0 : Math.sin(clamp((ph - 0.12) / 0.12, 0, 1) * Math.PI) + Math.sin(clamp((ph - 0.3) / 0.12, 0, 1) * Math.PI);
  const look = c.still ? 1 : env(ph, 0.02, 0.14, 0.55, 0.7);
  if (it && it.work === "present") {
    // holds it out beside the head like a little sign; the other hand opens toward it. Short arms
    // cannot point across the round body, so the gesture comes from its own side.
    const h = itemHand(it), sx = h ? 1 : -1, r = it.show || [0, 0, 0];
    holdOne(P, 1, it, h, sx * 1.12, 1.66 + 0.02 * tap, 0.42, r[0], r[1], r[2]);
    const o = 1 - h;
    hunch(P, 1, o);
    reach(P, o, -sx * 0.5, 1.6 + 0.03 * tap, 0.92);
    if (o) P.free1 = false; else P.free0 = false;
    P.hy += sx * 0.3 * look; P.hz += -sx * 0.05; P.cam = 1 - look * 0.8;
    P.gx = sx * 0.9; P.gy = 0.1; P.gk = look;
    P.smile += 0.15; P.wing += 0.1;
    return;
  }
  let tx, ty, tz;
  if (c.itemAt) [tx, ty, tz] = c.itemAt;
  const hand = c.itemAt ? (tx < 0 ? 0 : 1) : freeHand(c), sx = hand ? 1 : -1;
  keep(P, c, hand);
  if (hand) P.s1z += 0.05; else P.s0z += 0.05;
  if (c.itemAt) reach(P, hand, tx, Math.max(ty, 1.0) + 0.04 * tap, tz + 0.06 * tap, true);
  else reach(P, hand, sx * 1.22, 1.75 + 0.03 * tap, 0.55 + 0.06 * tap, true);
  if (hand) P.free1 = false; else P.free0 = false;
  if (c.itemAt) {
    P.yaw += sx * 0.1; P.lookAt = look > 0.5 ? [tx, ty, tz] : null; P.cam = 1 - look;
    P.hy += clamp(Math.atan2(tx, tz + 1.2), -0.7, 0.7) * look * 0.8; P.hx += clamp(0.12 * (2.7 - ty), -0.2, 0.3) * look;
  } else {
    P.hy += sx * 0.5 * look; P.yaw += sx * 0.12; P.hz -= sx * 0.03;
    P.gx = sx * 0.8; P.gy = 0.15; P.gk = look; P.cam = 1 - look;
  }
  P.smile += 0.1; P.wing += 0.1;
};

// Holds the thing out to you with a small bow; open hands without one.
A.offer = (P, c) => {
  const it = c.item;
  const push = c.still ? 1 : 0.6 + 0.4 * Math.sin((c.e % 4) / 4 * TAU - 1.2);
  P.lean += 0.06 + 0.03 * push; P.hx += 0.04; P.hz += 0.05; P.cam = 1; P.smile += 0.15;
  if (it && it.mount === "both") {
    hunch(P);
    const r = it.offer || [0.15, 0, 0];
    holdTwo(P, 1, it, 0, (it.offerY || 1.5) + 0.02 * push, (it.offerZ || 1.08) + 0.05 * push, r[0], r[1], r[2]);
  } else if (it && it.mount === "hand" && !it.carryOnly) {
    const h = itemHand(it), sx = h ? 1 : -1;
    hunch(P, 1, h);
    const r = it.show || it.carry || [0, 0, 0];
    holdOne(P, 1, it, h, sx * 0.38, 1.62 + 0.02 * push, 0.95 + 0.05 * push, r[0], r[1], r[2]);
    hunch(P, 1, 1 - h); reach(P, 1 - h, -sx * 0.3, 1.34, 0.86);
    if (h) P.free0 = false; else P.free1 = false;
  } else if (it && it.carryOnly) {
    keep(P, c);
    if (P.free0) { hunch(P, 1, 0); reach(P, 0, -0.5, 1.58, 0.92 + 0.04 * push); P.free0 = false; }
  } else {
    // open hands held out to you
    hunch(P);
    reach(P, 0, -0.5, 1.58, 0.92 + 0.04 * push); reach(P, 1, 0.5, 1.58, 0.92 + 0.04 * push);
    P.free0 = P.free1 = false;
    if (it && it.mount === "float") { P.gy = 0.6; P.gk = 0.5; }
  }
  if (c.comp && c.comp.mount === "hand" && !(it && it.mount === "both")) carry(P, c, 2, c.comp);
};

// Reading: the item tilted up toward the face, eyes running along a line and back.
function readPose(P, c, it, n = 1) {
  hunch(P);
  const r = it.read || [-0.72, 0, 0];
  const lineT = c.e % 1.9, line = Math.floor(c.e / 1.9);
  const scan = c.still ? 0 : lineT < 1.55 ? -0.5 + lineT / 1.55 : 0.5 - (lineT - 1.55) / 0.35;
  holdTwo(P, n, it, 0, (it.readY || 1.5) + 0.01 * c.breath, it.readZ || 0.98, r[0], r[1], r[2]);
  P.hx += 0.3; P.hy += scan * 0.08; P.cam = 0;
  P.gx = scan * 0.8; P.gy = -0.75 - (line % 4) * 0.04; P.gk = 1;
  P.lean += 0.03;
}

// Writing: the notebook in the far hand, tilted up, the pencil moving along its lines. The tip is
// put on the page first and the hand follows the pencil, so the pencil really touches the paper.
// the notebook sits right of centre: a right hand on short arms cannot write further left
export const WRITE = { w: 0.3, lines: [0.11, 0.03, -0.05, -0.13], period: 2.2, book: [0.12, 1.45, 0.9, -0.85, 0, 0.1] };
export function writeTip(e, still) {
  if (still) return { x: 0.02, line: 1, k: 0.55, lift: 0 };
  const per = WRITE.period, n = WRITE.lines.length;
  const cycle = e % (per * n + 1.2), line = Math.min(n - 1, Math.floor(cycle / per));
  const into = cycle - line * per, back = cycle > per * n;
  const k = clamp(into / (per - 0.35), 0, 1);
  const lift = back || into > per - 0.35 ? 1 : 0;
  return { x: -WRITE.w / 2 + WRITE.w * (back ? 1 - clamp((cycle - per * n) / 1.2, 0, 1) : k), line: back ? n - 1 : line, k, lift };
}
// An item whose local +y is aimed along the unit vector (lx, ly, lz), as an XYZ Euler.
function aimY(lx, ly, lz) {
  const a = Math.atan2(lx, lz), b = Math.atan2(Math.hypot(lx, lz), ly);
  const sa = Math.sin(a), ca = Math.cos(a), sb = Math.sin(b), cb = Math.cos(b);
  // R = Ry(a) · Rx(b) written as Rx · Ry · Rz
  return [Math.atan2(sb, ca * cb), Math.asin(clamp(sa * cb, -1, 1)), Math.atan2(-sa * sb, ca)];
}
function writePose(P, c, book, pen, nb, np, writing) {
  hunch(P);
  const [bx, by0, bz, rx, ry, rz] = WRITE.book, by = by0 + 0.008 * c.breath;
  place(P, nb, bx, by, bz, rx, ry, rz);
  rotate(gripOf(book, 0), rx, ry, rz, v3);
  reach(P, 0, bx + v3[0], by + v3[1], bz + v3[2]);
  hold(P, 0, nb);
  const tip = writeTip(c.e, c.still);
  const wig = c.still ? 0 : 0.012 * Math.sin(c.e * 31) * (1 - tip.lift);
  // between lines and while reading, the pencil rests just above the page
  const lift = writing ? tip.lift * 0.05 : 0.05;
  const page = rotate([tip.x, WRITE.lines[tip.line] + wig, (book.pageZ || 0.03) + lift], rx, ry, rz);
  const tx = bx + page[0], ty = by + page[1], tz = bz + page[2];
  // the pencil leans up and to the right, the way a right hand holds it; the hand is up the shaft
  const L = [0.62, 0.72, 0.1], l = Math.hypot(L[0], L[1], L[2]);
  const lx = L[0] / l, ly = L[1] / l, lz = L[2] / l;
  const [ex, ey, ez] = aimY(lx, ly, lz);
  const g = gripOf(pen, 1);
  rotate(g, ex, ey, ez, v3);
  // the tip is at local (0, -tip, 0): put it on the page
  const tipLocal = rotate([0, -(pen.tip || 0.31) * (pen.scale || 1), 0], ex, ey, ez);
  const ox = tx - tipLocal[0], oy = ty - tipLocal[1], oz = tz - tipLocal[2];
  place(P, np, ox, oy, oz, ex, ey, ez);
  reach(P, 1, ox + v3[0], oy + v3[1], oz + v3[2]);
  hold(P, 1, np);
  P.hx += 0.32; P.cam = 0; P.lean += 0.03;
  P.lookAt = [tx, ty, tz];
}

// Quiet work: reads the notebook, now and then writes a line, now and then looks up at you.
A.focus = (P, c) => {
  const it = c.item;
  if (!it || it.mount === "float" || it.mount === "head" || it.mount === "wear" || (it.mount === "ground" && it.work !== "type")) {
    // nothing to work with: hands together, eyes down, thinking it through
    hunch(P); reach(P, 0, -0.17, 1.3, 0.9); reach(P, 1, 0.17, 1.3, 0.9); P.free0 = P.free1 = false;
    P.hx += 0.22; P.cam = 0; P.gy = -0.7; P.gk = 1;
    if (it && it.mount === "ground") { P.lookAt = null; }
    return;
  }
  const work = it.work || "show";
  if (work === "notebook" || work === "pencil") {
    const nb = work === "notebook" ? 1 : 2, np = 3 - nb;
    const book = nb === 1 ? it : c.comp, pen = np === 1 ? it : c.comp;
    if (!book || !pen) { readPose(P, c, it); return; }
    const ph = c.still ? 6 : c.e % 12;
    const writing = ph > 4.2 && ph < 8.2;
    writePose(P, c, book, pen, nb, np, writing);
    if (!writing) { P.gx = c.still ? 0 : 0.5 * Math.sin(c.e * 1.4); P.gy = -0.7; P.gk = 1; P.lookAt = null; }
    const up = c.still ? 0 : env(ph, 8.6, 9.1, 10.1, 10.7);
    P.hx -= 0.3 * up; P.cam = up; P.gk *= 1 - up; P.smile += 0.25 * up;
    if (up > 0.3) P.lookAt = null;
    return;
  }
  workPose(P, c, it, work);
};
A.read = (P, c) => {
  const it = c.item;
  if (it && (it.work === "read" || it.work === "notebook" || it.mount === "both")) { readPose(P, c, it); return; }
  if (it && it.mount === "hand") { workPose(P, c, it, it.work || "show"); return; }
  A.focus(P, c);
};
A.write = (P, c) => {
  const it = c.item;
  if (it && (it.work === "pencil" || it.work === "notebook") && c.comp) {
    const nb = it.work === "notebook" ? 1 : 2;
    writePose(P, c, nb === 1 ? it : c.comp, nb === 1 ? c.comp : it, nb, 3 - nb, true);
    return;
  }
  if (it && it.work === "paint") { workPose(P, c, it, "paint"); return; }
  A.focus(P, c);
};

// The ways the working hands use a thing.
function workPose(P, c, it, work) {
  const e = c.e, still = c.still;
  if (it.carryOnly) { keep(P, c); P.hx += 0.1; P.lookAt = null; P.cam = 0.6; return; }
  if (work === "read") { readPose(P, c, it); return; }
  if (work === "type") {
    // at the little desk: both hands on the keys, a key pressed now and then, a pause to look
    hunch(P);
    const k0 = still ? 0 : Math.max(0, Math.sin(e * 13.1)) * 0.03, k1 = still ? 0 : Math.max(0, Math.sin(e * 11.7 + 1.7)) * 0.03;
    const pause = still ? 0 : env(e % 7, 4.6, 5, 6.2, 6.6);
    reach(P, 0, -0.21, 1.22 + k0 * (1 - pause) + 0.07 * pause, 0.9);
    reach(P, 1, 0.21, 1.22 + k1 * (1 - pause) + 0.07 * pause, 0.9);
    P.free0 = P.free1 = false;
    P.hx += 0.2 - 0.14 * pause; P.cam = pause; P.gy = -0.3; P.gk = 1 - pause; P.lean += 0.04;
    return;
  }
  if (work === "inspect") {
    // the magnifier up before one eye, which swims large behind the glass
    hunch(P, 1, 1);
    const drift = still ? 0 : 0.03 * Math.sin(e * 0.9);
    const r = it.inspect || [0, 0, 0];
    holdOne(P, 1, it, 1, 0.68 + drift, 2.0, 0.86, r[0], r[1], r[2]);
    P.hx += 0.1; P.hz += 0.05; P.cam = 1; P.magnify = [1, 1.3];
    // looking through a lens: a mood may not turn the face away from it
    P.lockHead = true;
    hunch(P, 1, 0); reach(P, 0, -0.2, 1.26, 0.88); P.free0 = false;
    return;
  }
  if (work === "compass") {
    hunch(P);
    const r = it.read || [0, 0, 0];
    holdOne(P, 1, it, 1, 0.22, 1.5 + 0.01 * c.breath, 0.98, r[0], r[1], r[2]);
    reach(P, 0, -0.2, 1.44, 0.98); P.free0 = false;
    P.yaw += still ? 0 : 0.1 * Math.sin(e * 0.7);
    P.hx += 0.34; P.cam = 0; P.lookAt = [P.p1x, P.p1y + 0.06, P.p1z];
    return;
  }
  if (work === "tinker") {
    // a few patient turns of the wrench, a look, again
    hunch(P);
    const ph = e % 3.2, turning = still ? 0.5 : env(ph, 0, 0.3, 2.1, 2.5);
    const swing = still ? 0 : Math.sin(ph * TAU / 0.9) * 0.35 * turning;
    const r = it.tinker || [0, 0, 0];
    holdOne(P, 1, it, 1, 0.3, 1.5, 1.0, r[0], r[1], r[2] + swing);
    reach(P, 0, -0.22, 1.42, 0.94); P.free0 = false;
    P.hx += 0.26; P.cam = 0; P.lookAt = [0.0, 1.62, 1.12];
    return;
  }
  if (work === "photo") {
    hunch(P);
    const r = it.offer || [0, 0, 0];
    const flash = still ? 0 : env(e % 3.4, 2.4, 2.45, 2.55, 2.75);
    holdTwo(P, 1, it, 0, 1.66 - 0.02 * flash, 1.04, r[0], r[1], r[2]);
    P.hx += 0.14; P.close = Math.max(P.close, 0.55 * flash); P.cam = 0.6;
    return;
  }
  if (work === "paint") {
    // the palette on the far arm, the brush drawing a slow loop in the air
    const palette = it.id === "palette" ? it : c.comp && c.comp.id === "palette" ? c.comp : null;
    const brush = it.id === "brush" ? it : c.comp && c.comp.id === "brush" ? c.comp : null;
    hunch(P, 0.7);
    if (palette) { const r = palette.carry || [0, 0, 0]; holdOne(P, palette === it ? 1 : 2, palette, 0, -0.7, 1.32, 0.66, r[0], r[1], r[2]); }
    if (brush) {
      const a = still ? 0.6 : e * 1.25;
      const x = 0.42 + 0.15 * Math.cos(a), y = 1.8 + 0.1 * Math.sin(a) + 0.05 * Math.sin(a * 2), z = 0.95;
      const r = brush.paint || brush.carry || [0, 0, 0];
      holdOne(P, brush === it ? 1 : 2, brush, 1, x, y, z, r[0], r[1], r[2] + 0.2 * Math.sin(a));
      P.lookAt = [x - 0.05, y + 0.32, z + 0.12];
    }
    P.cam = 0.15;
    return;
  }
  if (work === "listen") { A.listen(P, c); return; }
  // show: hold it up a little and look at it
  if (it.mount === "both") {
    hunch(P);
    const r = it.read || [-0.4, 0, 0];
    holdTwo(P, 1, it, 0, (it.readY || 1.5) + 0.01 * c.breath, it.readZ || 1.0, r[0], r[1], r[2]);
  } else if (it.mount === "hand") {
    const h = itemHand(it), sx = h ? 1 : -1;
    hunch(P, 1, h);
    const r = it.show || it.carry || [0, 0, 0];
    holdOne(P, 1, it, h, sx * 0.42, 1.66 + 0.01 * c.breath, 0.92, r[0], r[1], r[2]);
    hunch(P, 1, 1 - h); reach(P, 1 - h, -sx * 0.2, 1.3, 0.9);
    if (h) P.free0 = false; else P.free1 = false;
  }
  P.hx += 0.22; P.cam = 0; P.lookAt = [P.p1x, P.p1y + 0.1, P.p1z];
}

// A hand to the chin, eyes up and aside, an idea turning above (the gear floats there itself).
A.think = (P, c) => {
  const hand = freeHand(c), sx = hand ? 1 : -1;
  keep(P, c, hand);
  hunch(P, 1, hand);
  const ph = c.e % 3;
  const tap = c.still ? 0 : Math.max(0, Math.sin(ph * TAU / 1.5)) ** 3 * (ph < 1.5 ? 1 : 0);
  reach(P, hand, sx * 0.3, 1.74 + 0.03 * tap, 0.72);
  if (hand) P.free1 = false; else P.free0 = false;
  const other = 1 - hand;
  if (other ? P.free1 : P.free0) { hunch(P, 1, other); reach(P, other, -sx * 0.32, 1.32, 0.86); if (other) P.free1 = false; else P.free0 = false; }
  P.hx += -0.17; P.hz += sx * 0.06; P.hy += -sx * 0.1;
  P.gx = -sx * 0.45; P.gy = 0.75; P.gk = 1; P.cam = 0;
  P.smile -= 0.15; P.round += 0.2;
};

// Still, turned toward the voice, a hand by the side of the face, small "mm-hm" nods.
A.listen = (P, c) => {
  const it = c.item;
  if (it && it.id === "microphone") {
    // holds the microphone out toward you
    hunch(P, 1, 1);
    const r = it.offer || [0, 0, 0];
    holdOne(P, 1, it, 1, 0.3, 1.66, 1.04, r[0], r[1], r[2]);
    hunch(P, 1, 0); reach(P, 0, -0.2, 1.3, 0.9); P.free0 = false;
  } else {
    const hand = freeHand(c), sx = hand ? 1 : -1;
    keep(P, c, hand);
    reach(P, hand, sx * 0.98, 2.06, 0.3);
    if (hand) P.free1 = false; else P.free0 = false;
  }
  const ph = c.e % 3.4;
  const nod = c.still ? 0 : Math.sin(clamp((ph - 2.2) / 0.5, 0, 1) * Math.PI);
  // a louder voice draws it in: leans closer, wings lift, the nods come more often
  const v = c.voice || 0;
  P.lean += 0.07 + 0.04 * v; P.hz += 0.12; P.hx += (0.1 + 0.08 * v) * nod + 0.04; P.hy += 0.04;
  P.wing += 0.25 + 0.35 * v; P.cam = 1; P.smile += 0.1;
};

// Talking: a mouth that follows the voice, a hand that explains, a nod on the stressed words.
A.speak = (P, c) => {
  const it = c.item, e = c.e;
  const syll = c.still ? 0.5 : c.voice > 0.02 ? c.voice : Math.max(0, Math.sin(e * 9.5) * 0.6 + Math.sin(e * 4.1 + 1) * 0.4);
  const phrase = c.still ? 1 : env(e % 4.6, 0, 0.2, 3.5, 3.9);
  P.speak = c.voice > 0.02 ? c.voice : syll * phrase;
  if (it && it.id === "microphone") {
    // the microphone just in front of the mouth, never into the face
    hunch(P, 1, 1); P.s1y += 0.1;
    const r = it.speak || it.carry || [0, 0, 0];
    holdOne(P, 1, it, 1, 0.3, 1.86, 0.98, r[0], r[1], r[2]);
    P.hx += 0.12;
  } else {
    const hand = freeHand(c), sx = hand ? 1 : -1;
    keep(P, c, hand);
    // the explaining hand: out, palm-up, back in, in phrases
    const g = c.still ? 0.5 : 0.5 + 0.5 * Math.sin(e * TAU / 2.3);
    hunch(P, 0.6, hand);
    reach(P, hand, sx * (0.72 + 0.12 * g), 1.42 + 0.14 * g, 0.72 + 0.1 * g);
    if (hand) P.free1 = false; else P.free0 = false;
  }
  const beat = c.still ? 0 : Math.max(0, Math.sin(e * TAU / 1.15)) ** 4;
  P.hx += 0.06 * beat; P.hz += c.still ? 0 : 0.04 * Math.sin(e * 0.9);
  P.cam = 1; P.lean += 0.02;
};

// Leans in, hands open toward you, a slow "there, there"; with something two-handed (the heart),
// holds it to the chest and then offers it a little.
A.reassure = (P, c) => {
  const it = c.item, e = c.e;
  const pat = c.still ? 0 : Math.sin(e * TAU / 1.8);
  P.lean += 0.1; P.hx += 0.06; P.hz += 0.1; P.cam = 1; P.smile += 0.2;
  if (it && it.mount === "both") {
    hunch(P);
    const give = c.still ? 0.5 : env(e % 5, 1.2, 2, 3.6, 4.4);
    const r = it.hug || [0.08, 0, 0];
    holdTwo(P, 1, it, 0, (it.hugY || 1.3) + 0.06 * give + 0.01 * pat, (it.hugZ || 0.98) + 0.07 * give, r[0], r[1], r[2]);
  } else {
    keep(P, c);
    if (P.free0) { hunch(P, 1, 0); reach(P, 0, -0.62, 1.42 + 0.03 * pat, 0.85); P.free0 = false; }
    if (P.free1) { hunch(P, 1, 1); reach(P, 1, 0.62, 1.42 + 0.03 * pat, 0.85); P.free1 = false; }
  }
};

// Hops of joy with a crouch before and a soft landing after, arms up, wings beating; the hop
// comes once a cycle, so it reads as glad rather than frantic.
A.celebrate = (P, c) => {
  const it = c.item, e = c.e, still = c.still;
  const per = 1.7, ph = (e % per) / per;
  // crouch, stretch on take-off, round in the air, stretch before touching down, squash on
  // landing: overlapping smooth envelopes, so no two neighbouring frames disagree (the first
  // version switched from stretch to squash in one frame at touchdown)
  let lift = 0, sq = 1, tuckLegs = 0;
  if (!still) {
    const q = clamp((ph - 0.15) / 0.35, 0, 1);
    lift = Math.sin(q * Math.PI) * 0.36;
    tuckLegs = Math.sin(q * Math.PI);
    sq = 1 - 0.08 * env(ph, 0.0, 0.1, 0.12, 0.2) + 0.06 * env(ph, 0.13, 0.2, 0.22, 0.32)
      + 0.03 * env(ph, 0.4, 0.46, 0.47, 0.51) - 0.06 * env(ph, 0.49, 0.55, 0.58, 0.74);
  }
  P.lift += lift; P.squash *= sq; P.l0x -= 0.45 * tuckLegs; P.l1x -= 0.45 * tuckLegs;
  const up = still ? 1 : 0.75 + 0.25 * Math.sin(ph * TAU);
  if (it && it.mount === "both") {
    hunch(P);
    const r = it.offer || [0.15, 0, 0];
    holdTwo(P, 1, it, 0, 1.56 + 0.08 * up, 1.06, r[0], r[1], r[2]);
  } else {
    const h = itemHand(it);
    for (const i of [0, 1]) {
      const sx = i ? 1 : -1;
      // held up beside the head, never over it (the arms are too short to lift past it), and as
      // far out as the item is wide, tipped away from the face
      if (i === h) {
        const r = it.raise || it.carry || [0, 0, 0], ext = it.extent ? Math.max(it.extent[0], it.extent[2]) : 0.25;
        holdOne(P, 1, it, i, sx * Math.min(1.3, 1.02 + 0.7 * ext), 1.9 - 0.5 * ext + 0.06 * up, 0.4, r[0], r[1], r[2] - sx * 0.3);
      }
      else if (i) { P.a1z = 2.45 + 0.2 * up; P.a1x = 0.2; P.free1 = false; }
      else { P.a0z = -(2.45 + 0.2 * up); P.a0x = 0.2; P.free0 = false; }
    }
  }
  P.wing += 0.55; P.flap = still ? 0.3 : 0.9; P.smile += 0.35; P.open += 0.25; P.cam = 1;
  P.hx -= 0.08; P.hz += still ? 0 : 0.05 * Math.sin(e * 3.7);
};

// Step-touch in time: weight from foot to foot, the free foot lifting, arms swinging, a head bob.
A.dance = (P, c) => {
  const it = c.item, e = c.e, still = c.still;
  const ph = still ? 0.25 : (e / 1.1) % 1, s = Math.sin(ph * TAU);
  const hop = still ? 0 : Math.abs(s);
  P.bx += 0.1 * s; P.roll += -0.09 * s; P.by += 0.03 * hop;
  P.l0x = -0.4 * Math.max(0, -s); P.l1x = -0.4 * Math.max(0, s);
  P.l0z = -0.12 * Math.max(0, -s); P.l1z = 0.12 * Math.max(0, s);
  P.hx += 0.07 * Math.abs(Math.cos(ph * TAU)); P.hz += 0.08 * s;
  keep(P, c);
  if (P.free1) { P.a1z = 0.9 + 0.35 * s; P.a1x = -0.5 - 0.3 * s; P.free1 = false; }
  if (P.free0) { P.a0z = -0.9 + 0.35 * s; P.a0x = -0.5 + 0.3 * s; P.free0 = false; }
  P.flap = 0.45; P.wing += 0.2; P.smile += 0.3; P.cam = 1; P.close = Math.max(P.close, 0.2);
};

// Rises off the plinth on beating wings, feet dangling a beat behind.
A.float = (P, c) => {
  const e = c.e, still = c.still;
  const rise = still ? 1 : ramp(e, 0, 1.1);
  P.lift += (0.75 + (still ? 0 : 0.09 * Math.sin(e * 1.7))) * rise;
  const lag = still ? 0 : Math.sin(e * 1.7 - 0.9);
  P.l0x = 0.18 + 0.1 * lag; P.l1x = 0.12 + (still ? 0 : 0.1 * Math.sin(e * 1.7 - 1.3));
  P.lean -= 0.04; P.wing += 0.45; P.flap = 1;
  keep(P, c);
  if (P.free0) { P.a0z = -0.75 - 0.08 * lag; P.free0 = false; }
  if (P.free1) { P.a1z = 0.75 + 0.08 * lag; P.free1 = false; }
  P.cam = 1; P.smile += 0.15;
};

// A slow stretch: arms up, up on the toes, a yawn at the top, arms down, a satisfied breath.
A.stretch = (P, c) => {
  const it = c.item, still = c.still;
  const ph = still ? 1.8 : c.e % 5.4;
  const up = env(ph, 0.1, 1.4, 2.7, 3.7);
  const yawn = env(ph, 1.0, 1.6, 2.5, 3.0);
  const sway = still ? 0 : Math.sin(ph * 2.2) * 0.05 * env(ph, 1.2, 1.5, 2.5, 2.8);
  P.squash *= 1 + 0.07 * up; P.lift += 0.03 * up; P.hx -= 0.24 * up; P.roll += sway;
  // a hand that holds something keeps it low (lifting a mug or a book over the head would bring
  // it down on the face); the free arms stretch up
  keep(P, c);
  if (P.free1) { P.a1z = lerp(0.3, 2.75, up); P.a1x = lerp(0, 0.25, up); P.free1 = false; }
  if (P.free0) { P.a0z = -lerp(0.3, 2.75, up); P.a0x = lerp(0, 0.25, up); P.free0 = false; }
  P.open += 0.85 * yawn; P.round += 0.75 * yawn; P.close = Math.max(P.close, 0.92 * yawn + 0.35 * env(ph, 3.6, 4.0, 5.0, 5.4));
  P.wing += 0.5 * up; P.flap = 0.12 + 0.3 * up; P.cam = 1 - yawn;
  const sigh = env(ph, 3.6, 4.1, 4.8, 5.3);
  P.s0y -= 0.04 * sigh; P.s1y -= 0.04 * sigh; P.by -= 0.02 * sigh;
};

// Tea: the mug cupped in both hands, brought up as the head bows to meet it, a sip with eyes
// closed, down again, a contented breath.
const SIP = { period: 6.4 };
export function sipPhase(e, still) {
  const ph = still ? 2.8 : e % SIP.period;
  return { ph, up: env(ph, 1.0, 2.2, 3.5, 4.4), sip: env(ph, 2.0, 2.4, 3.3, 3.6), ahh: env(ph, 4.3, 4.8, 5.8, 6.3) };
}
A.sip = (P, c) => {
  const mug = c.item && c.item.id === "mug" ? c.item : c.comp && c.comp.id === "mug" ? c.comp : null;
  const n = mug === c.item ? 1 : 2;
  if (mug !== c.item && c.item && (c.item.mount === "both" || c.item.mount === "hand")) tuck(P, c, 1, c.item, 0);
  const { up, sip, ahh } = sipPhase(c.e, c.still);
  P.hx += 0.36 * up; P.lean += 0.05 * up;
  if (mug) {
    hunch(P, 1);
    P.s0y += 0.15 * up; P.s1y += 0.15 * up;
    holdTwo(P, n, mug, 0, lerp(1.38, 1.98, up) + 0.008 * c.breath, lerp(1.0, 0.98, up), -0.45 * sip - 0.08 * up, mug.sipYaw || 0, 0);
  }
  P.close = Math.max(P.close, 0.95 * sip + 0.5 * ahh);
  P.smile += 0.3 * ahh; P.by -= 0.02 * ahh; P.s0y -= 0.03 * ahh; P.s1y -= 0.03 * ahh;
  P.cam = 1 - up; P.gy = -0.5; P.gk = up * (1 - sip);
  P.blush += 0.15 * ahh;
};

// Dozes standing: head resting on the pillow, slow deep breaths, a little nod now and then.
A.sleep = (P, c) => {
  const it = c.item, still = c.still;
  P.close = 1; P.cam = 0; P.wing -= 0.35; P.flap = 0.04;
  const nod = still ? 0 : Math.max(0, Math.sin(c.e * TAU / 7)) ** 8;
  P.hx += 0.3 + 0.05 * nod; P.hz += 0.1; P.lean += 0.05;
  P.by += 0.012 * c.breath; P.smile += 0.1; P.emote = "zzz";
  if (it && it.mount === "both") {
    hunch(P);
    const r = it.hug || [0.08, 0, 0];
    holdTwo(P, 1, it, 0, (it.sleepY || it.hugY || 1.3) + 0.012 * c.breath, it.sleepZ || it.hugZ || 0.95, r[0], r[1], r[2]);
  } else {
    keep(P, c);
    if (P.free0) { P.a0z = -0.12; P.a0x = -0.12; P.free0 = false; }
    if (P.free1) { P.a1z = 0.12; P.a1x = -0.12; P.free1 = false; }
  }
};

export const ACTIONS = A;

// Breathing rate per mood (seconds per breath).
export function breathPeriod(w) {
  return 3.6 + 1.6 * w.sleepy + 0.8 * w.sad - 0.5 * w.joy - 0.4 * w.surprised + 0.4 * w.focused;
}

export function bodyPose(action, c, w) {
  const P = neutral();
  life(P, c);
  // a mood that wants stillness quiets the idle drift
  const quiet = 1 - 0.55 * (w.focused || 0) - 0.3 * (w.sleepy || 0);
  P.hy *= quiet; P.hx *= quiet;
  (A[action] || A.idle)(P, c);
  const armsFree = action === "idle" || action === "nod" || action === "peek" || action === "float";
  const hy = P.hy, hz = P.hz;
  moodPosture(P, c, w, armsFree);
  if (P.lockHead) { P.hy = hy; P.hz = hz; }
  if (action === "sleep" || action === "stretch") { P.hz = clamp(P.hz, -0.25, 0.25); }
  P.hx = clamp(P.hx, -0.45, 0.55);
  return P;
}

// ---- the flying head: no arms, so wings and tilts do the talking and things float before it ----
export function headPose(action, c, w) {
  const P = neutral();
  const t = c.t, e = c.e, still = c.still, br = c.breath;
  P.lift = 0.08 * Math.sin(t * 1.6) * (still ? 0 : 1);
  P.hy = 0.1 * noise(t * 0.21, 1) * (still ? 0 : 1); P.hx = 0.03 * noise(t * 0.3, 3) * (still ? 0 : 1);
  P.flap = 0.25; P.cam = 0.85; P.squash = 1 + 0.01 * br;
  const it = c.item;
  const a = action;
  if (a === "wave") { const ph = (e % 2.6) / 2.6; P.wing1 = 1.1 + 0.35 * Math.sin(e * TAU * 1.6) * env(ph, 0, 0.08, 0.62, 0.72); P.hz -= 0.08; P.cam = 1; P.smile += 0.15; }
  if (a === "nod") { const ph = (e % 2.2) / 2.2; P.hx += 0.24 * (Math.sin(clamp(ph / 0.22, 0, 1) * Math.PI) + 0.75 * Math.sin(clamp((ph - 0.26) / 0.22, 0, 1) * Math.PI)); }
  if (a === "peek") { const out = env((e % 4.4) / 4.4, 0.05, 0.25, 0.72, 0.92); P.hz -= 0.3 * out; P.bx = 0.35 * out; P.wing0 += 0.6 * out; P.open += 0.1 * out; P.round += 0.5 * out; }
  if (a === "point") { const look = env((e % 4.2) / 4.2, 0.02, 0.14, 0.55, 0.7); P.wing1 = 0.9; P.hy += 0.45 * look; P.gx = 0.8; P.gk = look; P.cam = 1 - look; }
  if (a === "offer") { P.hx += 0.12; P.wing = 0.3; P.cam = 1; P.smile += 0.15; }
  if (a === "focus" || a === "read" || a === "write") { P.hx += 0.3; P.cam = 0; P.gy = -0.8; P.gk = 1; if (a === "read") { P.gx = still ? 0 : Math.sin(e * 2.2) * 0.6; } }
  if (a === "think") { P.hx -= 0.18; P.hz += 0.08; P.gx = -0.45; P.gy = 0.75; P.gk = 1; P.cam = 0; P.wing1 = 0.5; }
  if (a === "listen") { P.hz += 0.16; P.wing += 0.4; P.cam = 1; const ph = e % 3.4; P.hx += 0.1 * (still ? 0 : Math.sin(clamp((ph - 2.2) / 0.5, 0, 1) * Math.PI)); }
  if (a === "speak") {
    const syll = still ? 0.5 : c.voice > 0.02 ? c.voice : Math.max(0, Math.sin(e * 9.5) * 0.6 + Math.sin(e * 4.1 + 1) * 0.4);
    P.speak = c.voice > 0.02 ? c.voice : syll * (still ? 1 : env(e % 4.6, 0, 0.2, 3.5, 3.9));
    P.hx += 0.05 * (still ? 0 : Math.max(0, Math.sin(e * TAU / 1.15)) ** 4); P.cam = 1;
  }
  if (a === "reassure") { P.hx += 0.1; P.hz += 0.1; P.wing = 0.4; P.cam = 1; P.smile += 0.2; }
  if (a === "celebrate") {
    const ph = (e % 1.7) / 1.7, q = clamp((ph - 0.14) / 0.36, 0, 1);
    P.lift += still ? 0.2 : Math.sin(q * Math.PI) * 0.45; P.wing = 1; P.flap = 1; P.smile += 0.35; P.open += 0.25;
    P.hz += still ? 0 : 0.08 * Math.sin(e * 3.7);
  }
  if (a === "dance") { const s = still ? 0 : Math.sin(e / 1.1 * TAU); P.hz += 0.16 * s; P.bx = 0.15 * s; P.hx += 0.06 * Math.abs(s); P.flap = 0.5; P.close = 0.2; P.smile += 0.3; }
  if (a === "float") { P.lift += 0.45 * (still ? 1 : ramp(e, 0, 1.1)); P.flap = 1; P.wing = 0.5; }
  if (a === "stretch") { const ph = still ? 1.8 : e % 5.4, up = env(ph, 0.1, 1.4, 2.7, 3.7), yawn = env(ph, 1.0, 1.6, 2.5, 3.0); P.wing = 1.2 * up; P.hx -= 0.2 * up; P.squash *= 1 + 0.05 * up; P.open += 0.85 * yawn; P.round += 0.75 * yawn; P.close = 0.92 * yawn; }
  if (a === "sip") { const { up, sip } = sipPhase(e, still); P.hx += 0.3 * up; P.close = 0.95 * sip; P.cam = 1 - up; }
  if (a === "sleep") { P.close = 1; P.hx += 0.25; P.hz += 0.12; P.lift -= 0.4; P.flap = 0.04; P.wing = -0.4; P.emote = "zzz"; P.cam = 0; }
  // Things it holds float before it, kept up by the same light it floats on: in front of the
  // chin to read or work, out toward you to offer, a lens over one eye, a mug that rises to the
  // mouth, a pillow under the head; otherwise beside it, bobbing.
  const bob = still ? 0 : 0.05 * Math.sin(t * 1.3);
  const air = (n, spec) => spec && (spec.mount === "hand" || spec.mount === "both");
  if (!it && a === "sip" && c.comp && c.comp.id === "mug") {
    // tea with nothing else chosen: the mug alone rises to the mouth
    const { up, sip } = sipPhase(e, still);
    place(P, 2, 0, lerp(-1.25, -0.56, up) + bob * (1 - up), lerp(0.8, 1.06, up), -0.45 * sip, 0.9, 0);
  }
  if (it) {
    const two = it.mount === "both", mug = it.id === "mug" ? 1 : c.comp && c.comp.id === "mug" ? 2 : 0;
    if (a === "sip" && mug) {
      const { up, sip } = sipPhase(e, still);
      place(P, mug, 0, lerp(-1.25, -0.56, up) + bob * (1 - up), lerp(0.8, 1.06, up), -0.45 * sip, 0.9, 0);
      if (mug === 2 && air(1, it)) place(P, 1, 1.34, -1.24 + bob, 0.4, 0, -0.3, 0);
    } else if (a === "sleep" && it.id === "pillow") {
      // low enough for a sleepy, bowed head: its front corner dips as it nods off
      place(P, 1, 0, -1.42, 0.1, 0, 0, 0);
    } else if (it.id === "balloon") {
      // the balloon rides beside the head on its string, clear of the face and the wing
      place(P, 1, 1.42, -1.25 + bob, 0.35, 0, 0, -0.18);
    } else if ((a === "focus" || a === "read" || a === "write") && it.work === "inspect") {
      // the lens floats over the eye on its right (the head is bowed: the eye sits a little low)
      place(P, 1, 0.38, -0.22 + bob * 0.3, 1.08, 0, 0, 0.15); P.magnify = [1, 1.3];
    } else if ((a === "speak" || a === "listen") && it.id === "microphone") {
      if (a === "speak") place(P, 1, 0.12, -0.78, 1.04, 0.25, 0, 0.2);
      else place(P, 1, 0.3, -0.85 + bob, 1.2, 1.15, 0, 0);
    } else if (a === "focus" || a === "read" || a === "write") {
      // just under the chin and in front of it: the taller the thing stands up, the lower it rides
      const drop = it.top != null ? Math.max(0, it.top - 0.3) : 0;
      if (air(1, it)) place(P, 1, two ? 0 : 0.5, -0.98 - drop + bob * 0.4, 1.08, two ? -0.62 : 0.3, 0, two ? 0 : -0.3);
      if (c.comp && air(2, c.comp)) {
        // a pencil writes on its own, a brush paints a loop beside
        const k = still ? 0.5 : (e * 0.45) % 1;
        place(P, 2, -0.15 + 0.3 * k, -0.9 + 0.02 * Math.sin(e * 30) * (still ? 0 : 1), 1.08, -0.6, 0, -0.5);
      }
    } else if (a === "offer" || a === "celebrate" || a === "reassure") {
      const drop = it.top != null ? Math.max(0, it.top - 0.3) : 0;
      if (air(1, it)) place(P, 1, 0, -1.1 - drop + bob, 1.12, two ? 0.1 : 0, 0, 0);
      if (c.comp && air(2, c.comp)) place(P, 2, -1.32, -1.22 + bob, 0.4, 0, 0.3, 0);
    } else {
      // beside it and a little below, where a tilt or a nod of the boxy head cannot reach
      if (air(1, it)) place(P, 1, 1.34, -1.24 + bob, 0.4, 0, -0.3, 0);
      if (c.comp && air(2, c.comp)) place(P, 2, -1.34, -1.24 - bob, 0.4, 0, 0.3, 0);
    }
  }
  // moods
  P.hx += 0.08 * w.focused + 0.06 * w.worried + 0.12 * w.sleepy + 0.12 * w.shy + 0.2 * w.sad - 0.08 * w.surprised - 0.12 * w.proud;
  P.hz += 0.13 * w.curious + 0.06 * w.sleepy + 0.1 * w.shy + 0.12 * w.affectionate;
  P.hy += 0.08 * w.curious - 0.14 * w.shy;
  P.wing += 0.3 * w.joy - 0.4 * w.sleepy + 0.6 * w.surprised + 0.3 * w.proud - 0.2 * w.shy - 0.5 * w.sad + 0.25 * w.determined;
  P.flap += 0.3 * w.joy + 0.35 * w.surprised;
  P.lift += 0.05 * w.joy * Math.abs(Math.sin(t * Math.PI * 1.25)) * (still ? 0 : 1) - 0.12 * w.sad - 0.08 * w.sleepy;
  P.hx = clamp(P.hx, -0.45, a === "sleep" ? 0.4 : 0.5);
  return P;
}
