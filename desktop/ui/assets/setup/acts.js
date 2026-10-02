// Hero's acting: one small scene per step change, idle life between them, and reactions to what is
// typed. Every step has its own move tied to what the step is about, so no two transitions in a row
// look the same; going back is its own pair of moves. Each act names a "delivery" moment and a point
// on screen: the new step grows out of that point (his core, the chosen prop, the plane's landing).
import { tween, ease, reducedMotion } from "./stage3d.js";
import { RoundedBoxGeometry } from "./vendor/RoundedBoxGeometry.js";

export function makeActs(c) {
  const { THREE, stage, guide, GROUND, isPortrait } = c;
  const d = guide.d;
  const V = (x, y, z) => new THREE.Vector3(x, y, z);
  const clamp = (x, a, b) => Math.max(a, Math.min(b, x));
  const smooth = (k) => k * k * (3 - 2 * k);
  const seg = (raw, a, b) => clamp((raw - a) / (b - a), 0, 1);

  // ---- materials and small props ----
  const mint = new THREE.MeshStandardMaterial({ color: 0x5ee2c4, emissive: 0x5ee2c4, emissiveIntensity: 0.6, roughness: 0.4 });
  const mintGlow = new THREE.MeshBasicMaterial({ color: 0x9ff7e3, toneMapped: false });
  const bronze = new THREE.MeshPhysicalMaterial({ color: 0xb8864b, metalness: 0.85, roughness: 0.28, clearcoat: 0.5 });
  const dark = new THREE.MeshPhysicalMaterial({ color: 0x1d2024, roughness: 0.35, clearcoat: 0.8 });
  const paper = new THREE.MeshStandardMaterial({ color: 0xe9fbf6, roughness: 0.6, side: THREE.DoubleSide, emissive: 0x2e9c82, emissiveIntensity: 0.15 });
  const sparkTex = (() => {
    const cv = document.createElement("canvas"); cv.width = cv.height = 64;
    const g = cv.getContext("2d"), r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    r.addColorStop(0, "rgba(220,255,245,1)"); r.addColorStop(0.3, "rgba(94,226,196,.6)"); r.addColorStop(1, "rgba(94,226,196,0)");
    g.fillStyle = r; g.fillRect(0, 0, 64, 64);
    return new THREE.CanvasTexture(cv);
  })();
  const spark = (s) => { const m = new THREE.Sprite(new THREE.SpriteMaterial({ map: sparkTex, blending: THREE.AdditiveBlending, depthWrite: false, toneMapped: false, transparent: true, opacity: 0 })); m.scale.setScalar(s); return m; };
  const hide = (o) => { o.visible = false; o.scale.setScalar(0.0001); };

  // How it runs: a little laptop and a box hover either side of the plinth, like holograms.
  const runsProps = new THREE.Group(); stage.scene.add(runsProps);
  const laptop = new THREE.Group();
  {
    const base = new THREE.Mesh(new RoundedBoxGeometry(0.07, 0.005, 0.05, 2, 0.002), dark); laptop.add(base);
    const lid = new THREE.Group(); lid.position.set(0, 0.0025, -0.025); lid.rotation.x = -0.25; laptop.add(lid);
    const back = new THREE.Mesh(new RoundedBoxGeometry(0.07, 0.046, 0.004, 2, 0.002), dark); back.position.y = 0.023; lid.add(back);
    const scr = new THREE.Mesh(new THREE.PlaneGeometry(0.062, 0.038), mint); scr.position.set(0, 0.023, 0.0025); lid.add(scr);
    laptop.userData.glow = scr.material = mint.clone();
  }
  const box = new THREE.Group();
  {
    const cube = new THREE.Mesh(new RoundedBoxGeometry(0.05, 0.05, 0.05, 2, 0.006), bronze); box.add(cube);
    const edges = new THREE.LineSegments(new THREE.EdgesGeometry(new THREE.BoxGeometry(0.054, 0.054, 0.054)), new THREE.LineBasicMaterial({ color: 0x5ee2c4, transparent: true, opacity: 0.8 }));
    box.add(edges); box.userData.glow = edges.material;
  }
  const discM = new THREE.MeshBasicMaterial({ color: 0x5ee2c4, transparent: true, opacity: 0.35, toneMapped: false, depthWrite: false });
  for (const [o, x] of [[laptop, -0.26], [box, 0.26]]) {
    const holder = new THREE.Group(); holder.position.set(x, 0.07, 0.06); runsProps.add(holder);
    holder.add(o);
    const disc = new THREE.Mesh(new THREE.RingGeometry(0.035, 0.042, 40), discM); disc.rotation.x = -Math.PI / 2; disc.position.y = -0.03; holder.add(disc);
    o.userData.holder = holder;
  }
  laptop.position.y = 0.0; box.position.y = 0.0;
  runsProps.visible = false;

  // Model: a gear that spins above his head while he thinks, and a spark for the "aha".
  const gear = (() => {
    const sh = new THREE.Shape(); const n = 10, r1 = 0.3, r2 = 0.4;
    for (let i = 0; i < n * 2; i++) {
      const a0 = (i / (n * 2)) * Math.PI * 2, a1 = ((i + 1) / (n * 2)) * Math.PI * 2, r = i % 2 ? r1 : r2;
      if (i === 0) sh.moveTo(Math.cos(a0) * r, Math.sin(a0) * r); else sh.lineTo(Math.cos(a0) * r, Math.sin(a0) * r);
      sh.lineTo(Math.cos(a1) * r, Math.sin(a1) * r);
    }
    const hole = new THREE.Path(); hole.absarc(0, 0, 0.12, 0, Math.PI * 2, true); sh.holes.push(hole);
    const g = new THREE.Mesh(new THREE.ExtrudeGeometry(sh, { depth: 0.08, bevelEnabled: true, bevelSize: 0.02, bevelThickness: 0.02, bevelSegments: 2 }), mint.clone());
    g.geometry.center(); return g;
  })();
  // above the crest's tip (y 4.05 in body units) with room to spin
  d.overhead.add(gear); gear.position.y = 0.3; hide(gear);
  const aha = spark(1.6); d.overhead.add(aha); aha.position.y = 0.3;

  // Spending: two bronze coins to juggle.
  const coinGeo = new THREE.CylinderGeometry(0.22, 0.22, 0.05, 32);
  const coins = [0, 1].map(() => { const m = new THREE.Mesh(coinGeo, bronze); m.rotation.x = Math.PI / 2; d.bob.add(m); hide(m); return m; });

  // Telegram: a paper plane, held, then thrown at the card.
  const plane = (() => {
    const g = new THREE.BufferGeometry();
    // nose at +z; two wings and a keel, folded a little
    const p = [0, 0, 0.5, -0.32, 0.05, -0.3, 0, 0, -0.22, 0, 0, 0.5, 0, 0, -0.22, 0.32, 0.05, -0.3, 0, 0, 0.5, 0, -0.12, -0.25, 0, 0, -0.22];
    g.setAttribute("position", new THREE.Float32BufferAttribute(p, 3)); g.computeVertexNormals();
    const m = new THREE.Mesh(g, paper); m.castShadow = true; return m;
  })();
  stage.scene.add(plane); hide(plane);
  const trail = [0, 1, 2].map(() => { const s = spark(0.03); stage.scene.add(s); return s; });

  // Advanced: a wrench in his hand and a bolt-gear floating in front of him.
  const wrench = (() => {
    const g = new THREE.Group();
    const h = new THREE.Mesh(new RoundedBoxGeometry(0.1, 0.62, 0.06, 2, 0.03), bronze); h.position.y = -0.31; g.add(h);
    const head = new THREE.Mesh(new THREE.TorusGeometry(0.11, 0.045, 10, 24, Math.PI * 1.55), bronze); head.position.y = -0.68; head.rotation.z = Math.PI * 0.72; g.add(head);
    return g;
  })();
  d.arms[1].add(wrench); wrench.position.set(0, -0.55, 0.12); wrench.rotation.x = -1.3; hide(wrench);
  const bolt = (() => { const m = gear.clone(); m.material = bronze; m.scale.setScalar(0.5); return m; })();
  d.bob.add(bolt); bolt.position.set(0.15, 1.3, 1.25); hide(bolt);
  const boltSparks = [0, 1, 2].map(() => { const s = spark(0.35); d.bob.add(s); return s; });

  // Summary: a scroll he reads down, a mint bar following his eyes.
  const scroll = (() => {
    const cv = document.createElement("canvas"); cv.width = 128; cv.height = 192;
    const g = cv.getContext("2d");
    g.fillStyle = "#e8f6f1"; g.fillRect(0, 0, 128, 192);
    for (let i = 0; i < 5; i++) {
      g.fillStyle = "#2e9c82"; g.beginPath(); g.arc(20, 30 + i * 34, 6, 0, Math.PI * 2); g.fill();
      g.fillStyle = "rgba(20,40,36,.55)"; g.fillRect(34, 26 + i * 34, 50 + ((i * 37) % 40), 8);
    }
    const tex = new THREE.CanvasTexture(cv); tex.colorSpace = THREE.SRGBColorSpace;
    const grp = new THREE.Group();
    const sheet = new THREE.Mesh(new THREE.PlaneGeometry(0.9, 1.3), new THREE.MeshStandardMaterial({ map: tex, roughness: 0.8, side: THREE.DoubleSide }));
    sheet.position.y = -0.65; grp.add(sheet);
    const rod = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 1.05, 16), bronze); rod.rotation.z = Math.PI / 2; grp.add(rod);
    const bar = new THREE.Mesh(new THREE.PlaneGeometry(0.8, 0.12), new THREE.MeshBasicMaterial({ color: 0x5ee2c4, transparent: true, opacity: 0.45, toneMapped: false, depthWrite: false }));
    bar.position.z = 0.005; sheet.add(bar);
    grp.userData = { sheet, bar };
    return grp;
  })();
  // held below the face (the head's underside is at 1.86), so he reads it without hiding behind it
  d.bob.add(scroll); scroll.position.set(0, 1.8, 1.15); scroll.rotation.x = -0.3; hide(scroll);

  // A thumb for the thumbs-up: the mitten has none until it needs one.
  const thumb = new THREE.Mesh(new THREE.CapsuleGeometry(0.06, 0.14, 4, 10), d.arms[1].children[1].material);
  d.arms[1].add(thumb); thumb.position.set(0, -0.56, 0.12); hide(thumb);

  // ---- arms, head and body offsets, blended on top of the guide's own pose ----
  const arm = [{ x: 0, y: 0, z: 0, k: 0, tk: 0 }, { x: 0, y: 0, z: 0, k: 0, tk: 0 }];
  const armTo = (i, x, z, k, y) => { Object.assign(arm[i], { x, z, y: y || 0, tk: k == null ? 1 : k }); };
  const armsFree = () => { arm[0].tk = 0; arm[1].tk = 0; };
  const off = { yaw: 0, pitch: 0, roll: 0, lean: 0, leanF: 0, spin: 0 };
  const want = { yaw: 0, pitch: 0, roll: 0, lean: 0, leanF: 0 };
  // what the running act asks for; the frame loop adds it to everything else
  const act = { yaw: 0, pitch: 0, roll: 0, lean: 0 };
  let acting = false, pointer = { x: 0, y: 0 }, leanUntil = 0, thumbUntil = 0, glanceUntil = 0;
  const idle = { next: 2.5, cur: null, t: 0, last: "" };
  let clock = 0;

  window.addEventListener("pointermove", (e) => {
    pointer.x = (e.clientX / window.innerWidth) * 2 - 1;
    pointer.y = (e.clientY / window.innerHeight) * 2 - 1;
  }, { passive: true });

  const cardSide = () => (isPortrait() ? { yaw: 0, pitch: 0.35, gx: 0, gy: -1 } : { yaw: 0.55, pitch: 0.1, gx: 1, gy: -0.2 });

  stage.onFrame((dt, t) => {
    clock = t;
    const still = reducedMotion();
    // idle life: one small thing at a time, never the same twice running
    let iy = 0, ip = 0, ir = 0, ihop = 0, iflap = 0;
    if (!acting && !still) {
      idle.next -= dt;
      if (!idle.cur && idle.next <= 0) {
        const pool = ["look", "glance", "hop", "flutter", "stretch", "tilt"].filter((n) => n !== idle.last);
        idle.cur = pool[Math.floor(Math.random() * pool.length)]; idle.t = 0; idle.last = idle.cur;
      }
      if (idle.cur) {
        const D = { look: 2.2, glance: 1.6, hop: 0.6, flutter: 1.0, stretch: 1.4, tilt: 1.4 }[idle.cur];
        idle.t += dt; const p = clamp(idle.t / D, 0, 1), bump = Math.sin(p * Math.PI);
        if (idle.cur === "look") iy = Math.sin(p * Math.PI * 2) * 0.55 * bump;
        if (idle.cur === "glance") { const s = cardSide(); iy = s.yaw * bump; ip = s.pitch * bump; guide.gaze.x = s.gx * bump; guide.gaze.y = s.gy * bump; }
        if (idle.cur === "hop") { ihop = Math.max(0, Math.sin(p * Math.PI)) * 0.025; }
        if (idle.cur === "flutter") iflap = bump * 0.9;
        if (idle.cur === "stretch") { armTo(0, -2.7, 0.35, bump); armTo(1, -2.7, 0.35, bump); }
        if (idle.cur === "tilt") ir = 0.22 * bump;
        if (p >= 1) { if (idle.cur === "stretch") armsFree(); idle.cur = null; idle.next = 2.5 + Math.random() * 3; }
      }
    } else if (idle.cur) { if (idle.cur === "stretch") armsFree(); idle.cur = null; idle.next = 3; }
    if (!acting && !still && idle.cur !== "glance") {
      // the head follows the cursor a little; the eyes follow it more
      guide.gaze.x += (pointer.x - guide.gaze.x) * (1 - Math.exp(-5 * dt));
      guide.gaze.y += (-pointer.y - guide.gaze.y) * (1 - Math.exp(-5 * dt));
    }
    // leaning in to watch a field being typed into
    const leaning = t < leanUntil;
    const s = cardSide();
    want.lean = leaning ? (isPortrait() ? 0 : -0.14) : 0;
    want.leanF = leaning ? (isPortrait() ? 0.16 : 0.05) : 0;
    const lookCard = leaning || t < glanceUntil;
    want.lean += act.lean;
    want.yaw = (lookCard ? s.yaw : (!acting && !still ? pointer.x * 0.3 : 0)) + iy + act.yaw;
    want.pitch = (lookCard ? s.pitch : (!acting && !still ? pointer.y * 0.12 : 0)) + ip + act.pitch;
    want.roll = ir + act.roll;
    if (lookCard) { guide.gaze.x = s.gx; guide.gaze.y = s.gy; }
    const a = 1 - Math.exp(-7 * dt);
    for (const k of ["yaw", "pitch", "roll", "lean", "leanF"]) off[k] += (want[k] - off[k]) * (still ? 1 : a);
    d.head.rotation.y += off.yaw; d.head.rotation.x += off.pitch; d.head.rotation.z += off.roll;
    d.bob.rotation.z += off.lean; d.bob.rotation.x += off.leanF;
    guide.root.rotation.y += off.spin;
    if (ihop > 0 && guide.hop === 0) d.root.position.y += ihop;
    if (iflap > 0) d.wings.forEach((w) => w.fs.forEach((pv, k2) => { pv.rotation.y += iflap * 0.6 * Math.sin(t * 20 + k2); }));
    // thumbs-up
    const th = t < thumbUntil;
    if (th) armTo(1, -1.75, 0.15, 1, -0.2); else if (arm[1].thumb) armTo(1, 0, 0, 0);
    arm[1].thumb = th;
    thumb.visible = th; thumb.scale.setScalar(th ? 1 : 0.0001);
    // arms last, over everything
    arm.forEach((A, i) => {
      A.k += (A.tk - A.k) * (still ? 1 : 1 - Math.exp(-9 * dt));
      if (A.k < 0.003) return;
      const r = d.arms[i].rotation;
      r.x += (A.x - r.x) * A.k; r.z += ((i === 0 ? -1 : 1) * A.z - r.z) * A.k; r.y += (A.y - r.y) * A.k;
    });
    gear.rotation.z += dt * 3.2;
    // the runs props bob gently while they are up
    if (runsProps.visible) { laptop.userData.holder.position.y = 0.07 + 0.006 * Math.sin(t * 2.1); box.userData.holder.position.y = 0.07 + 0.006 * Math.sin(t * 2.1 + 1.4); }
  });

  // ---- helpers for acts ----
  const pv = new THREE.Vector3();
  function screenOf(obj) {
    obj.updateWorldMatrix(true, false); obj.getWorldPosition(pv).project(stage.camera);
    const { W, H } = stage.size; return { x: (pv.x * 0.5 + 0.5) * W, y: (-pv.y * 0.5 + 0.5) * H };
  }
  const coreAnchor = new THREE.Object3D(); coreAnchor.position.set(0, 1.18, 0.75); d.bob.add(coreAnchor);
  const handAnchor = (i) => d.arms[i].children[1];
  const pop = (o, on, dur) => {
    o.visible = true;
    const s0 = o.scale.x;
    return tween(dur || 0.35, (k) => { const v = on ? s0 + (1 - s0) * ease.back(k) : s0 * (1 - k); o.scale.setScalar(Math.max(0.0001, v)); }, (k) => k).then(() => { if (!on) hide(o); });
  };
  function clearProps(keep) {
    const all = [gear, wrench, bolt, scroll, ...coins];
    all.forEach((o) => { if (o !== keep && o.visible) pop(o, false, 0.25); });
    if (keep !== runsProps && runsProps.visible) {
      tween(0.25, (k) => { runsProps.scale.setScalar(Math.max(0.0001, 1 - k)); }).then(() => { runsProps.visible = false; runsProps.scale.setScalar(1); });
    }
    plane.visible = false; trail.forEach((s) => { s.material.opacity = 0; });
    aha.material.opacity = 0; boltSparks.forEach((s) => { s.material.opacity = 0; });
  }
  const glowTo = (to, dur) => { const g0 = guide.moodTo.glow; return tween(dur, (k) => { guide.mood.glow = guide.moodTo.glow = g0 + (to - g0) * k; }); };
  const crestM = d.face.crest.material;
  const crestTo = (to, dur) => { const e0 = crestM.emissiveIntensity; return tween(dur, (k) => { crestM.emissiveIntensity = e0 + (to - e0) * k; }); };

  // ---- the acts: (S, deliver) => Promise; call deliver(screenPoint) once ----
  const ACTS = {
    // waves hello, and the core comes up from dim to bright like someone waking a lamp
    welcome(S, deliver) {
      guide.wave(2.2);
      return tween(1.3, (k, raw) => {
        guide.mood.glow = guide.moodTo.glow = 0.4 + 1.8 * Math.sin(Math.min(1, raw / 0.7) * Math.PI * 0.5) - 0.8 * seg(raw, 0.7, 1);
        if (raw > 0.45) deliver(screenOf(coreAnchor));
      }, (k) => k);
    },
    // a laptop and a box pop up; he looks from one to the other and taps the chosen one
    runs(S, deliver) {
      runsProps.visible = true; runsProps.scale.setScalar(1);
      [laptop, box].forEach((o) => { o.scale.setScalar(0.0001); o.visible = true; });
      pop(laptop, true, 0.4);
      setTimeout(() => pop(box, true, 0.4), reducedMotion() ? 0 : 120);
      return tween(1.5, (k, raw) => {
        act.yaw = raw < 0.3 ? -0.45 * Math.sin(seg(raw, 0, 0.3) * Math.PI) : raw < 0.55 ? 0.45 * Math.sin(seg(raw, 0.3, 0.55) * Math.PI) : 0;
        if (raw > 0.55 && raw < 0.6) tapChoice(S.mode);
        if (raw > 0.72) deliver(screenOf(S.mode === "docker" ? box : laptop));
      }, (k) => k).then(() => { act.yaw = 0; });
    },
    // hand to chin, a gear turning over his head, then the spark: aha, and a little jump
    model(S, deliver) {
      armTo(1, -2.45, -0.35, 1);
      d.head.rotation.z += 0.0;
      pop(gear, true, 0.35);
      let jumped = false;
      return tween(1.6, (k, raw) => {
        act.roll = raw < 0.55 ? 0.2 : 0;
        if (raw > 0.55 && !jumped) {
          jumped = true; armsFree(); armTo(1, -2.9, 0.2, 1);
          guide.jump(0.38, 0.045); guide.feel("happy", 0.9);
          pop(gear, false, 0.2);
        }
        const q = seg(raw, 0.55, 0.85);
        aha.material.opacity = Math.sin(q * Math.PI);
        aha.scale.setScalar(0.6 + 2.2 * q);
        if (raw > 0.62) deliver(screenOf(aha));
        if (raw > 0.9) armsFree();
      }, (k) => k).then(() => { aha.material.opacity = 0; act.roll = 0; armsFree(); });
    },
    // two coins juggled hand to hand, the last one caught and held up
    limit(S, deliver) {
      // Low arcs in front of the body, at hand height: the first version tossed them over his face
      // and they passed through the front of the head. Head: |x| < 1.0, y 1.86..3.6, z < 0.84.
      const L = V(-0.6, 1.2, 1.1), R = V(0.6, 1.2, 1.1);
      // placed in the hands before they are shown, so no frame has them at the body's centre
      coins.forEach((m, j) => { m.position.copy(j ? R : L); m.visible = true; m.scale.setScalar(1); });
      return tween(1.7, (k, raw) => {
        const toss = 3 * raw; // three tosses
        coins.forEach((m, j) => {
          const ph = (toss + j * 0.5) % 1, dir = Math.floor(toss + j * 0.5) % 2;
          const a = dir ? R : L, b = dir ? L : R;
          m.position.lerpVectors(a, b, ph); m.position.y += Math.sin(ph * Math.PI) * 0.42;
          m.rotation.y = toss * 6 + j;
          if (raw > 0.82) { // the catch: both come to the right hand, one held up
            const q = seg(raw, 0.82, 1);
            m.position.lerp(V(1.5 + j * 0.06, 2.2 + j * 0.1, 0.9), q); // held up beside the head, clear of it
          }
        });
        const up = (s) => Math.max(0, Math.sin(s * Math.PI * 2));
        armTo(0, -1.1 - 0.7 * up(toss / 2), 0.25, raw < 0.82 ? 1 : 0.4);
        armTo(1, -1.1 - 0.7 * up(toss / 2 + 0.5), 0.25, 1);
        if (raw > 0.82) armTo(1, -2.3, 0.4, 1);
        guide.gaze.y = 0.6; act.pitch = -0.25;
        if (raw > 0.86) deliver(screenOf(coins[0]));
      }, (k) => k).then(() => { armsFree(); act.pitch = 0; setTimeout(() => coins.forEach((m) => pop(m, false, 0.25)), 600); });
    },
    // folds back his arm and throws a paper plane; it glides to the card and the step opens there
    telegram(S, deliver) {
      const hand = handAnchor(1);
      const start = new THREE.Vector3(), right = new THREE.Vector3(), up = V(0, 1, 0);
      right.setFromMatrixColumn(stage.camera.matrixWorld, 0);
      const toCam = new THREE.Vector3().subVectors(stage.camera.position, guide.root.position).setY(0).normalize();
      const end = guide.root.position.clone().add(isPortrait() ? up.clone().multiplyScalar(0.05).add(toCam.clone().multiplyScalar(0.5)) : right.clone().multiplyScalar(0.34).add(up.clone().multiplyScalar(0.3)).add(toCam.clone().multiplyScalar(0.15)));
      plane.visible = true; plane.scale.setScalar(0.06);
      return tween(1.5, (k, raw) => {
        // wind-up .0-.35, throw .35-.45, flight .45-1
        const wind = seg(raw, 0, 0.35), thr = seg(raw, 0.35, 0.45);
        // the wind-up goes out to the side, not over the head: raised straight back, the plane
        // in his hand passed through the top of his head
        armTo(1, -1.7 + 0.5 * thr, 1.65 - 1.2 * thr, 1);
        act.lean = 0.08 * wind - 0.14 * thr;
        if (raw < 0.42) { hand.getWorldPosition(start); plane.position.copy(start); plane.lookAt(start.clone().add(right)); }
        else {
          const f = seg(raw, 0.42, 1), e = ease.out(f);
          const p = new THREE.Vector3().lerpVectors(start, end, e); p.y += Math.sin(f * Math.PI) * 0.12;
          const prev = plane.position.clone(); plane.position.copy(p);
          if (p.distanceToSquared(prev) > 1e-9) plane.lookAt(p.clone().add(p.clone().sub(prev)));
          plane.rotateZ(Math.sin(f * 6) * 0.3);
          trail.forEach((s, j) => { s.position.lerp(p, 0.35 - j * 0.1); s.material.opacity = (1 - f) * 0.7; });
          plane.scale.setScalar(0.06 * (1 - seg(raw, 0.9, 1)) + 0.0001);
          if (raw > 0.8) deliver(screenOf(plane));
        }
        if (raw > 0.6) armsFree();
      }, (k) => k).then(() => { plane.visible = false; trail.forEach((s) => { s.material.opacity = 0; }); act.lean = 0; });
    },
    // pulls out a wrench and tightens a floating bolt; a few sparks fly
    advanced(S, deliver) {
      pop(wrench, true, 0.3); pop(bolt, true, 0.35);
      return tween(1.6, (k, raw) => {
        const turn = seg(raw, 0.25, 0.85);
        armTo(1, -1.35 + 0.25 * Math.sin(turn * Math.PI * 6), 0.15, 1, 0.3 * Math.sin(turn * Math.PI * 6));
        bolt.rotation.z = -turn * Math.PI * 2;
        boltSparks.forEach((s, j) => {
          const q = (turn * 3 + j / 3) % 1;
          s.position.set(0.15 + Math.cos(j * 2.1 + q * 3) * (0.2 + q * 0.4), 1.3 + q * 0.5, 1.3);
          s.material.opacity = turn > 0 && turn < 1 ? Math.sin(q * Math.PI) * 0.9 : 0;
        });
        guide.gaze.x = 0.3; guide.gaze.y = -0.4; act.pitch = 0.12;
        if (raw > 0.8) deliver(screenOf(bolt));
      }, (k) => k).then(() => { armsFree(); act.pitch = 0; boltSparks.forEach((s) => { s.material.opacity = 0; }); pop(wrench, false, 0.25); pop(bolt, false, 0.25); });
    },
    // unrolls a checklist and reads down it, nodding at each line
    summary(S, deliver) {
      scroll.visible = true; scroll.scale.set(1, 0.0001, 1);
      armTo(0, -1.45, -0.1, 1); armTo(1, -1.45, -0.1, 1);
      const { sheet, bar } = scroll.userData;
      return tween(2.0, (k, raw) => {
        scroll.scale.set(1, Math.max(0.0001, ease.out(seg(raw, 0, 0.25))), 1);
        const read = seg(raw, 0.25, 0.9);
        bar.position.y = 0.47 - read * 0.95;
        guide.gaze.y = 0.2 - read * 1.4; guide.gaze.x = 0;
        act.pitch = 0.15 + read * 0.25;
        guide.mood.nod = Math.max(0, Math.sin(read * Math.PI * 5)) * 0.35;
        if (raw > 0.5) deliver(screenOf(sheet));
      }, (k) => k).then(() => { guide.mood.nod = 0; act.pitch = 0; armsFree(); pop(scroll, false, 0.3); });
    },
    // powers up: the crest and the core glow, wings beat, he lifts a little off the plinth
    start(S, deliver) {
      deliver(screenOf(coreAnchor));
      crestTo(1.4, 1.2); glowTo(2.0, 1.2);
      return tween(1.4, (k, raw) => {
        guide.flap = Math.sin(raw * Math.PI) * 0.8;
        guide.lift = Math.sin(raw * Math.PI) * 0.025;
        armTo(0, -0.4, 0.9, Math.sin(raw * Math.PI)); armTo(1, -0.4, 0.9, Math.sin(raw * Math.PI));
      }, (k) => k).then(() => { guide.flap = 0; guide.lift = 0; armsFree(); });
    },
  };

  // Going back: he glances over his shoulder, or rewinds with a quick spin; alternated.
  let backTurn = 0;
  function back() {
    const v = backTurn++ % 2;
    if (v === 0) {
      return tween(0.9, (k, raw) => {
        const b = Math.sin(raw * Math.PI);
        off.spin = -1.9 * b; act.pitch = -0.1 * b; guide.gaze.x = -b;
      }, (k) => k).then(() => { off.spin = 0; act.pitch = 0; guide.gaze.x = 0; return guide.jump(0.3, 0.02); });
    }
    return tween(0.8, (k) => { off.spin = -Math.PI * 2 * k; guide.flap = Math.sin(k * Math.PI) * 0.6; }, ease.inOut).then(() => { off.spin = 0; guide.flap = 0; });
  }

  function tapChoice(mode) {
    const docker = mode === "docker";
    // laptop is on the viewer's left (his right hand, arm 0), the box on the right (arm 1)
    const i = docker ? 1 : 0;
    armTo(i, -0.75, 0.75, 1);
    setTimeout(() => armTo(i, 0, 0, 0), reducedMotion() ? 0 : 450);
    laptop.userData.glow.emissiveIntensity = docker ? 0.15 : 1.1;
    box.userData.glow.opacity = docker ? 1 : 0.25;
    const holder = (docker ? box : laptop).userData.holder;
    tween(0.4, (k, raw) => { holder.scale.setScalar(1 + 0.25 * Math.sin(raw * Math.PI)); }, (k) => k);
  }

  return {
    // run the act for step `id`; deliver(point) fires once when the new step should appear
    async play(id, S, dir, deliver, alive) {
      acting = true; idle.cur = null;
      guide.calm(); armsFree(); off.spin = 0; act.roll = 0;
      let given = false;
      const give = (p) => { if (!given && alive()) { given = true; deliver(p); } };
      clearProps(id === "runs" ? runsProps : null);
      if (id !== "start") { crestTo(0.12, 0.4); }
      try {
        if (dir < 0) { give(null); await back(); if (id === "runs") await ACTS.runs(S, () => {}); }
        else if (ACTS[id]) await ACTS[id](S, give);
      } finally {
        give(null);
        if (alive()) acting = false;
      }
    },
    // the step is shown without an act (first paint, reduced motion): props it implies appear still
    place(id, S) {
      clearProps(id === "runs" ? runsProps : null);
      if (id === "runs") { runsProps.visible = true; [laptop, box].forEach((o) => { o.visible = true; o.scale.setScalar(1); }); tapChoice(S.mode); }
    },
    tap: tapChoice,
    lean() { leanUntil = clock + 1.4; },
    thumbsUp() { thumbUntil = clock + 1.3; },
    glanceAtCard(s) { glanceUntil = clock + (s || 1.2); },
    get acting() { return acting; },
  };
}
