// The little staff (chibi subagents) and Daedalus himself (the brand avatar: dark rounded face,
// two eyes, a mint crest, mint wings — docs/brand/avatar-bot.png via remotion/src/v2/Mascot.tsx).
import * as THREE from "./vendor/three.module.min.js";

export const COL = { head: 0x25272d, mint: 0x5ee2c4, mint2: 0x3fbf9f, mint3: 0x2e9c82, bronze: 0xb8864b, boot: 0x2f3238 };

// Rounded box by pushing a sphere toward a cube; e < 1 flattens the faces, keeps round edges.
function roundedBlock(sx, sy, sz, e, seg) {
  const g = new THREE.SphereGeometry(1, seg || 64, Math.round((seg || 64) * 0.75));
  const p = g.attributes.position;
  const f = (v) => Math.sign(v) * Math.pow(Math.abs(v), e);
  for (let i = 0; i < p.count; i++) p.setXYZ(i, f(p.getX(i)) * sx, f(p.getY(i)) * sy, f(p.getZ(i)) * sz);
  g.computeVertexNormals();
  return g;
}

// A feather: a curved, bevelled blade (root at the origin, tip along +x), bent slightly in z.
function featherGeo(len, wid, bend) {
  const s = new THREE.Shape();
  s.moveTo(0, -wid * 0.35);
  s.bezierCurveTo(len * 0.35, -wid * 0.6, len * 0.8, -wid * 0.3, len, 0);
  s.bezierCurveTo(len * 0.75, wid * 0.55, len * 0.3, wid * 0.7, 0, wid * 0.35);
  s.closePath();
  const g = new THREE.ExtrudeGeometry(s, { depth: 0.035, bevelEnabled: true, bevelSize: 0.03, bevelThickness: 0.03, bevelSegments: 3, curveSegments: 18 });
  g.translate(0, 0, -0.0175);
  const p = g.attributes.position;
  for (let i = 0; i < p.count; i++) { const x = p.getX(i); p.setZ(i, p.getZ(i) + bend * (x / len) * (x / len)); }
  g.computeVertexNormals();
  return g;
}

function crestGeo() {
  const s = new THREE.Shape();
  s.moveTo(0, 0.44);
  s.quadraticCurveTo(-0.06, 0.22, -0.28, 0.0);
  s.lineTo(0.28, 0.0);
  s.quadraticCurveTo(0.06, 0.22, 0, 0.44);
  const g = new THREE.ExtrudeGeometry(s, { depth: 0.1, bevelEnabled: true, bevelSize: 0.035, bevelThickness: 0.035, bevelSegments: 3, curveSegments: 12 });
  g.translate(0, 0, -0.05);
  return g;
}

let shared = null;
function sharedParts() {
  if (shared) return shared;
  const headM = new THREE.MeshPhysicalMaterial({ color: COL.head, roughness: 0.38, metalness: 0.05, clearcoat: 1, clearcoatRoughness: 0.14 });
  const eyeM = new THREE.MeshPhysicalMaterial({ color: 0xf6f6f4, roughness: 0.2, clearcoat: 1, clearcoatRoughness: 0.05, emissive: 0x2a2a2a, emissiveIntensity: 0.4 });
  const pupilM = new THREE.MeshPhysicalMaterial({ color: 0x121316, roughness: 0.15, clearcoat: 1, clearcoatRoughness: 0.05 });
  const glintM = new THREE.MeshBasicMaterial({ color: 0xffffff, toneMapped: false });
  const bodyM = new THREE.MeshPhysicalMaterial({ color: COL.bronze, roughness: 0.3, metalness: 0.62, clearcoat: 0.6, clearcoatRoughness: 0.2 });
  const bellyM = new THREE.MeshPhysicalMaterial({ color: 0xd9a86a, roughness: 0.35, metalness: 0.45, clearcoat: 0.6 });
  const bootM = new THREE.MeshPhysicalMaterial({ color: COL.boot, roughness: 0.45, clearcoat: 0.5 });
  const soleM = new THREE.MeshStandardMaterial({ color: COL.mint3, roughness: 0.5 });
  const featherM = [COL.mint, COL.mint2, COL.mint3].map((c) => new THREE.MeshPhysicalMaterial({ color: c, roughness: 0.35, clearcoat: 0.5, side: THREE.DoubleSide, emissive: c, emissiveIntensity: 0.12 }));
  const mouthM = new THREE.MeshBasicMaterial({ color: COL.mint, toneMapped: false });
  const cheekM = new THREE.MeshBasicMaterial({ color: 0x5ee2c4, transparent: true, opacity: 0.16, blending: THREE.AdditiveBlending, toneMapped: false, depthWrite: false });
  const bodyGeo = (() => {
    const pts = [];
    const prof = [[0.0, 0.5], [0.3, 0.52], [0.52, 0.6], [0.66, 0.78], [0.72, 1.0], [0.7, 1.25], [0.6, 1.5], [0.44, 1.72], [0.28, 1.84], [0.0, 1.88]];
    for (const [r, y] of prof) pts.push(new THREE.Vector2(r, y));
    const curve = new THREE.SplineCurve(pts);
    const g = new THREE.LatheGeometry(curve.getPoints(40), 48);
    g.scale(1, 1, 0.9);
    return g;
  })();
  shared = {
    headM, eyeM, pupilM, glintM, bodyM, bellyM, bootM, soleM, featherM, mouthM, cheekM, bodyGeo,
    headGeo: roundedBlock(1.0, 0.86, 0.84, 0.42, 72),
    eyeGeo: new THREE.SphereGeometry(1, 32, 24),
    crest: crestGeo(),
    feathers: [featherGeo(0.95, 0.34, 0.08), featherGeo(1.18, 0.3, 0.1), featherGeo(1.06, 0.28, 0.12)],
    bigFeathers: [featherGeo(1.0, 0.3, 0.1), featherGeo(1.22, 0.28, 0.12), featherGeo(1.12, 0.26, 0.14), featherGeo(0.92, 0.24, 0.14)],
    armGeo: (() => { const g = new THREE.CapsuleGeometry(0.14, 0.34, 8, 16); g.translate(0, -0.26, 0); return g; })(),
    mitten: new THREE.SphereGeometry(0.17, 24, 16),
    legGeo: (() => { const g = new THREE.CapsuleGeometry(0.16, 0.26, 8, 16); g.translate(0, -0.22, 0); return g; })(),
    mouthGeo: new THREE.TorusGeometry(0.13, 0.028, 10, 24, Math.PI * 0.8),
    cheekGeo: new THREE.CircleGeometry(0.085, 24),
    ledGeo: new THREE.TorusGeometry(0.13, 0.035, 10, 32),
    ledCore: new THREE.CircleGeometry(0.09, 24),
  };
  return shared;
}

// The face: shared by the little ones and by Daedalus. Returns eye handles for blink and gaze.
function buildFace(parent, S, scale) {
  const hm = new THREE.Mesh(S.headGeo, S.headM);
  hm.castShadow = true;
  parent.add(hm);
  const eyes = [];
  for (const sx of [-1, 1]) {
    const e = new THREE.Group(); e.position.set(sx * 0.38, 0.03, 0.76); parent.add(e);
    const w = new THREE.Mesh(S.eyeGeo, S.eyeM); w.scale.set(0.2, 0.25, 0.1); e.add(w);
    const pv = new THREE.Group(); e.add(pv);
    const p = new THREE.Mesh(S.eyeGeo, S.pupilM); p.scale.set(0.11, 0.13, 0.05); p.position.z = 0.075; pv.add(p);
    const g1 = new THREE.Mesh(S.eyeGeo, S.glintM); g1.scale.set(0.035, 0.04, 0.01); g1.position.set(0.035, 0.05, 0.125); pv.add(g1);
    const g2 = new THREE.Mesh(S.eyeGeo, S.glintM); g2.scale.set(0.016, 0.016, 0.01); g2.position.set(-0.04, -0.05, 0.123); pv.add(g2);
    eyes.push({ e, pv });
  }
  const mouth = new THREE.Mesh(S.mouthGeo, S.mouthM);
  mouth.position.set(0, -0.28, 0.846); mouth.rotation.z = Math.PI + Math.PI * 0.1; mouth.scale.set(1, 0.8, 1);
  parent.add(mouth);
  for (const sx of [-1, 1]) {
    const ch = new THREE.Mesh(S.cheekGeo, S.cheekM); ch.position.set(sx * 0.6, -0.2, 0.846); ch.rotation.y = sx * 0.35; parent.add(ch);
  }
  const crest = new THREE.Mesh(S.crest, S.featherM[0]); crest.position.y = 0.8; crest.castShadow = true; parent.add(crest);
  return { eyes, mouth, crest };
}

// A little one, ~0.36 m tall at DS = 0.085 (design units -> metres).
export function makeChibi(DS) {
  const S = sharedParts();
  const root = new THREE.Group();
  const s = new THREE.Group(); s.scale.setScalar(DS); root.add(s);
  const bob = new THREE.Group(); s.add(bob);
  const cast = (m) => { m.castShadow = true; m.receiveShadow = true; return m; };
  const body = cast(new THREE.Mesh(S.bodyGeo, S.bodyM)); body.position.y = 0.1; bob.add(body);
  const belly = cast(new THREE.Mesh(S.eyeGeo, S.bellyM)); belly.scale.set(0.46, 0.44, 0.2); belly.position.set(0, 1.12, 0.52); bob.add(belly);
  // status light on the chest: amber while working, mint when done
  const ledM = new THREE.MeshBasicMaterial({ color: 0xffb35c, toneMapped: false });
  const led = new THREE.Mesh(S.ledGeo, ledM); led.position.set(0, 1.18, 0.7); bob.add(led);
  const ledCoreM = new THREE.MeshBasicMaterial({ color: 0xffb35c, transparent: true, opacity: 0.55, toneMapped: false });
  const ledCore = new THREE.Mesh(S.ledCore, ledCoreM); ledCore.position.set(0, 1.18, 0.71); bob.add(ledCore);
  const head = new THREE.Group(); head.position.y = 2.72; bob.add(head);
  const face = buildFace(head, S);
  const wings = [];
  for (const sx of [-1, 1]) {
    const wg = new THREE.Group(); wg.position.set(sx * 0.42, 1.55, -0.42); wg.scale.set(sx * 0.78, 0.78, 0.78); wg.rotation.set(0, sx * 0.6, sx * 0.35); bob.add(wg);
    const fs = S.feathers.map((geo, i) => {
      const pv = new THREE.Group(); pv.rotation.z = 0.5 - i * 0.42; wg.add(pv);
      pv.add(cast(new THREE.Mesh(geo, S.featherM[i])));
      return pv;
    });
    wings.push({ wg, fs, sx });
  }
  const arms = [];
  for (const sx of [-1, 1]) {
    const a = new THREE.Group(); a.position.set(sx * 0.62, 1.5, 0.05); bob.add(a);
    a.add(cast(new THREE.Mesh(S.armGeo, S.bodyM)));
    const m = cast(new THREE.Mesh(S.mitten, S.bodyM)); m.position.y = -0.62; a.add(m);
    arms.push(a);
  }
  const legs = [];
  for (const sx of [-1, 1]) {
    const l = new THREE.Group(); l.position.set(sx * 0.28, 0.72, 0); s.add(l);
    l.add(cast(new THREE.Mesh(S.legGeo, S.bootM)));
    const boot = cast(new THREE.Mesh(S.eyeGeo, S.bootM)); boot.scale.set(0.21, 0.15, 0.3); boot.position.set(0, -0.6, 0.07); l.add(boot);
    const sole = new THREE.Mesh(S.eyeGeo, S.soleM); sole.scale.set(0.2, 0.05, 0.29); sole.position.set(0, -0.7, 0.07); l.add(sole);
    legs.push(l);
  }
  // the card it carries, held in front with both mittens
  const card = new THREE.Group(); card.position.set(0, 1.25, 0.95); bob.add(card);
  const overhead = new THREE.Group(); overhead.position.set(0, 4.35, 0.55); bob.add(overhead);
  return { root, s, bob, head, face, wings, arms, legs, led, ledM, ledCoreM, card, overhead, DS };
}

// Pose the little one for one frame. o: { run (0..1), phase, t, seed, happy, carry, lookUp, gaze }
export function poseChibi(c, o) {
  const run = o.run, ph = o.phase, t = o.t, b = o.seed;
  const still = 1 - run;
  // alive when standing: breathing, a slow weight shift from foot to foot
  c.bob.position.y = run * Math.abs(Math.sin(ph)) * 0.24 + (o.hop || 0) + still * 0.025 * Math.sin(t * 2.3 + b);
  c.bob.rotation.x = run * 0.2 + still * 0.03 * Math.sin(t * 1.1 + b * 2);
  c.bob.rotation.z = run * 0.05 * Math.sin(ph) + still * 0.06 * Math.sin(t * 0.9 + b);
  c.legs[0].rotation.x = Math.sin(ph) * 0.85 * run + (o.legsUp || 0);
  c.legs[1].rotation.x = -Math.sin(ph) * 0.85 * run + (o.legsUp || 0);
  const carry = o.carry || 0, happy = o.happy || 0, wave = o.wave || 0, work = o.work || 0, shrug = o.shrug || 0;
  const swing = (1 - carry) * run;
  c.arms[0].rotation.set(-Math.sin(ph) * 0.9 * swing - carry * 1.25, 0, -0.3 - carry * -0.05 - happy * (1.9 + 0.35 * Math.sin(t * 9)) );
  c.arms[1].rotation.set(Math.sin(ph) * 0.9 * swing - carry * 1.25, 0, 0.3 + carry * -0.05 + happy * (1.9 + 0.35 * Math.sin(t * 9 + 1)) + wave * (2.3 + 0.4 * Math.sin(t * 11)));
  // working at the board: quick alternating reaches, as if moving cards
  if (work > 0) {
    const w1 = Math.max(0, Math.sin(t * 7.3 + b)), w2 = Math.max(0, Math.sin(t * 7.3 + b + Math.PI));
    c.arms[0].rotation.x += (-1.9 * w1 - 0.6) * work; c.arms[1].rotation.x += (-1.9 * w2 - 0.6) * work;
  }
  // shrug: both arms out, palms up, a little bob
  if (shrug > 0) { c.arms[0].rotation.z -= 1.1 * shrug; c.arms[1].rotation.z += 1.1 * shrug; c.arms[0].rotation.x -= 0.5 * shrug; c.arms[1].rotation.x -= 0.5 * shrug; }
  // explicit arm poses (reach up for a hand, slap a wall, point, hold a tablet overhead), blended in
  if (o.arms) o.arms.forEach((A, i) => {
    if (!A || !(A.k > 0)) return;
    const r = c.arms[i].rotation;
    r.x = r.x + (A.x - r.x) * A.k;
    r.z = r.z + ((i === 0 ? -1 : 1) * A.z - r.z) * A.k;
    r.y = r.y + ((A.y || 0) - r.y) * A.k;
  });
  c.head.rotation.x = -(o.lookUp || 0) + 0.05 * Math.sin(t * 2 + b) + run * 0.08;
  // idle glances around when nobody tells the head where to look
  c.head.rotation.y = o.headYaw != null ? o.headYaw : still * (0.35 * Math.sin(t * 0.37 + b * 2.1) + 0.12 * Math.sin(t * 1.3 + b));
  if (shrug > 0) c.head.rotation.z += 0.18 * shrug * Math.sin(t * 3);
  c.head.rotation.z = 0.07 * Math.sin(t * 1.3 + b) * (1 - run);
  const flut = run * 0.45 * Math.sin(t * 16 + b) + 0.09 * Math.sin((t / 2.6) * 2 * Math.PI + b) + (o.flap || 0) * 0.8 * Math.sin(t * 22 + b);
  c.wings.forEach((w) => w.fs.forEach((pv, k) => {
    pv.rotation.y = flut * (1 - k * 0.15) + 0.25 * run + (o.flap || 0) * 0.5;
    pv.rotation.x = -0.12 * k;
  }));
  const blinkEvery = 2.6 + (b % 1.3);
  const bc = ((t + b) % blinkEvery) / 0.15;
  const lid = bc < 1 ? Math.sin(bc * Math.PI) : 0;
  const gx = o.gazeX || 0, gy = o.gazeY || 0;
  c.face.eyes.forEach((e) => {
    e.e.scale.y = 1 - 0.92 * lid * (1 - happy * 0.3);
    e.pv.position.set(0.05 * gx + 0.015 * Math.sin(t * 0.9 + b), 0.05 * gy, 0);
  });
  // happy: eyes become little arcs (squash), mouth opens
  c.face.mouth.scale.set(1 + happy * 0.25, 0.8 + happy * 0.6, 1);
}

// Daedalus: the brand face, larger, with the brand wings at the sides of the face; he hovers.
export function makeMascot(size) {
  const S = sharedParts();
  const root = new THREE.Group();
  const s = new THREE.Group(); s.scale.setScalar(size); root.add(s);
  const head = new THREE.Group(); s.add(head);
  const face = buildFace(head, S);
  face.crest.scale.setScalar(1.25);
  const wings = [];
  for (const sx of [-1, 1]) {
    const wg = new THREE.Group(); wg.position.set(sx * 0.9, -0.12, -0.1); wg.scale.set(sx, 1, 1); head.add(wg);
    const fs = S.bigFeathers.map((geo, i) => {
      const pv = new THREE.Group(); pv.rotation.z = 0.62 - i * 0.36; wg.add(pv);
      const m = new THREE.Mesh(geo, S.featherM[Math.min(2, i)]); m.castShadow = true; pv.add(m);
      return pv;
    });
    wings.push({ wg, fs, sx });
  }
  // a soft mint glow disc under him, like the light he floats on
  const glow = new THREE.Mesh(new THREE.CircleGeometry(1.1, 48), new THREE.MeshBasicMaterial({ color: COL.mint, transparent: true, opacity: 0.0, toneMapped: false, depthWrite: false }));
  glow.rotation.x = -Math.PI / 2; glow.position.y = -1.3; s.add(glow);
  return { root, s, head, face, wings, glow };
}

export function poseMascot(m, o) {
  const t = o.t;
  m.s.position.y = 0.08 * Math.sin(t * 1.6);
  m.head.rotation.set(-(o.lookUp || 0) + 0.04 * Math.sin(t * 1.1), o.yaw || 0, 0.05 * Math.sin(t * 0.8) + (o.tilt || 0));
  const beat = 0.22 * Math.sin(t * 3.2) + (o.flap || 0) * 0.5 * Math.sin(t * 14);
  m.wings.forEach((w) => w.fs.forEach((pv, k) => {
    pv.rotation.y = beat * (1 - k * 0.12) - 0.2 + (o.point && w.sx === o.point ? -0.7 : 0);
    pv.rotation.x = -0.08 * k;
  }));
  const bc = ((t + 0.7) % 3.4) / 0.16;
  const lid = bc < 1 ? Math.sin(bc * Math.PI) : 0;
  m.face.eyes.forEach((e) => { e.e.scale.y = 1 - 0.92 * lid; e.pv.position.set(0.05 * (o.gazeX || 0), 0.05 * (o.gazeY || 0), 0); });
  m.face.mouth.scale.set(1 + (o.happy || 0) * 0.2, 0.8 + (o.happy || 0) * 0.5, 1);
}

// The setup guide: Daedalus with the full body of the staff (round bronze body, boots with mint
// soles) and the brand head on top. Same parts as the little ones, so the family reads as one,
// but the crest is the brand's larger one and the chest light is a mint core rather than a
// status lamp: the guide is never "working", it is always the one who is awake.
export function makeDaedalus(size) {
  const d = makeChibi(size);
  d.face.crest.scale.setScalar(1.25);
  d.face.crest.position.y = 0.78;
  d.ledM.color.setHex(COL.mint);
  d.ledCoreM.color.setHex(0xbffff0);
  d.ledCoreM.opacity = 0.9;
  // a soft halo around the core; additive, so it reads as light without a bloom pass
  const haloTex = (() => {
    const c = document.createElement("canvas"); c.width = c.height = 64;
    const g = c.getContext("2d");
    const r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    r.addColorStop(0, "rgba(140,255,225,1)"); r.addColorStop(0.35, "rgba(94,226,196,.45)"); r.addColorStop(1, "rgba(94,226,196,0)");
    g.fillStyle = r; g.fillRect(0, 0, 64, 64);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; return t;
  })();
  const haloM = new THREE.SpriteMaterial({ map: haloTex, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false, opacity: 0.75 });
  const halo = new THREE.Sprite(haloM); halo.scale.setScalar(0.95); halo.position.set(0, 1.18, 0.78);
  d.bob.add(halo);
  d.halo = halo; d.haloM = haloM;
  d.wings.forEach((w) => w.wg.scale.set(w.sx * 0.92, 0.92, 0.92));
  return d;
}

// Moods layered over poseChibi: worry turns the smile over and drops the gaze, a nod dips the
// head once. Kept separate so poseChibi stays the film's code.
export function poseDaedalus(d, o) {
  poseChibi(d, o);
  const worry = o.worry || 0, nod = o.nod || 0;
  const smile = Math.PI + Math.PI * 0.1, frown = Math.PI * 0.1;
  // turning the arc through the angles in between would show a sideways mouth, so it flips
  // at the midpoint and the flattening hides the jump
  d.face.mouth.rotation.z = worry > 0.45 ? frown : smile;
  if (worry > 0) d.face.mouth.scale.y *= Math.abs(worry - 0.45) * 1.6 + 0.12;
  d.face.mouth.position.y = -0.28 - 0.08 * worry;
  if (worry > 0) d.face.eyes.forEach((e) => { e.e.scale.y *= 1 - 0.18 * worry; e.pv.position.y -= 0.04 * worry; });
  d.head.rotation.x += 0.32 * nod + 0.12 * worry;
  d.head.rotation.z += 0.14 * worry * Math.sin((o.t || 0) * 2.2);
  const pulse = 0.75 + 0.25 * Math.sin((o.t || 0) * 2.4);
  d.haloM.opacity = (0.35 + 0.3 * (o.glow == null ? 1 : o.glow)) * pulse;
  d.halo.scale.setScalar(0.6 + 0.3 * (o.glow == null ? 1 : o.glow) * pulse);
}
