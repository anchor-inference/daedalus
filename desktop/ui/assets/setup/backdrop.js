// Scene dressing for the setup page: a light cone onto the plinth, dust in the air, a faint
// labyrinth on the floor and a few blurred app windows far behind. Everything is additive and
// dim on purpose: the wizard card sits over this and has to stay readable. All of it is built
// once; update() only moves uniforms and a few group offsets (seven draw calls in total).

// Per-step tint and strength. Small differences: the room changes mood, it does not change colour.
const ACCENTS = {
  welcome:  { cone: 0x5ee2c4, coneK: 1.00, dust: 0x5ee2c4, dustK: 1.00, floor: 0x5ee2c4 },
  runs:     { cone: 0x3fc5b4, coneK: 1.00, dust: 0x3fbfae, dustK: 1.00, floor: 0x3fbfae },
  model:    { cone: 0x56d4e6, coneK: 1.05, dust: 0x66d8ee, dustK: 1.05, floor: 0x4cc8dc },
  limit:    { cone: 0xe6c08a, coneK: 0.95, dust: 0xd9a869, dustK: 1.00, floor: 0xb8864b },
  telegram: { cone: 0x7fdcf0, coneK: 1.05, dust: 0x8be4e8, dustK: 1.05, floor: 0x6fd0d8 },
  advanced: { cone: 0xd9a869, coneK: 0.90, dust: 0xb8864b, dustK: 0.95, floor: 0xb8864b },
  summary:  { cone: 0x5ee2c4, coneK: 1.10, dust: 0x5ee2c4, dustK: 1.05, floor: 0x5ee2c4 },
  start:    { cone: 0x8ff5dc, coneK: 1.60, dust: 0x9ff7e2, dustK: 1.40, floor: 0x7eeed2 },
};

export function buildBackdrop(stage, opts) {
  const { THREE, GROUND, reduced } = opts;
  const scene = stage.scene;
  const isReduced = () => !!(reduced && reduced());
  const smooth = (k) => k * k * (3 - 2 * k);

  // Current and target accent, blended by one progress value so a change mid-tween just restarts
  // from wherever the colours are now.
  const cur = { cone: new THREE.Color(ACCENTS.welcome.cone), dust: new THREE.Color(ACCENTS.welcome.dust), floor: new THREE.Color(ACCENTS.welcome.floor), coneK: 1, dustK: 1 };
  const from = { cone: cur.cone.clone(), dust: cur.dust.clone(), floor: cur.floor.clone(), coneK: 1, dustK: 1 };
  const to = { cone: cur.cone.clone(), dust: cur.dust.clone(), floor: cur.floor.clone(), coneK: 1, dustK: 1 };
  let prog = 1;
  const ACCENT_TIME = 0.8;

  // ---- 1. the light cone ----
  const CONE_TOP = 2.4, CONE_R_TOP = 0.55, CONE_R_BOTTOM = 0.17;
  const coneH = CONE_TOP - GROUND;
  const coneU = { uColor: { value: cur.cone.clone() }, uK: { value: 1 }, uTime: { value: 0 }, uShimmer: { value: 1 } };
  const cone = new THREE.Mesh(
    new THREE.CylinderGeometry(CONE_R_TOP, CONE_R_BOTTOM, coneH, 56, 1, true),
    new THREE.ShaderMaterial({
      uniforms: coneU, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending, side: THREE.DoubleSide,
      vertexShader: `
        varying float vH; varying float vEdge; varying float vAng;
        void main() {
          vH = uv.y;                       // 1 at the top, 0 at the plinth
          vAng = atan(position.z, position.x);
          vec4 wp = modelMatrix * vec4(position, 1.0);
          vec3 n = normalize(mat3(modelMatrix) * normal);
          vec3 v = normalize(cameraPosition - wp.xyz);
          // seen edge-on the sheet is thick, seen face-on it is thin: fade the thin parts so the
          // cone reads as a volume of light, not as a glass cup
          vEdge = pow(abs(dot(n, v)), 1.4);
          gl_Position = projectionMatrix * viewMatrix * wp;
        }`,
      fragmentShader: `
        uniform vec3 uColor; uniform float uK, uTime, uShimmer;
        varying float vH; varying float vEdge; varying float vAng;
        void main() {
          float a = smoothstep(0.0, 0.18, vH) * (1.0 - smoothstep(0.55, 1.0, vH)) * 0.55 + smoothstep(0.0, 0.5, 1.0 - vH) * 0.0;
          a *= 0.25 + 0.75 * vEdge;
          float sh = 1.0 + uShimmer * 0.16 * sin(uTime * 0.6 + vAng * 3.0 + vH * 5.0) + uShimmer * 0.08 * sin(uTime * 1.1 - vAng * 5.0);
          a *= sh * uK * 0.2;
          gl_FragColor = vec4(mix(uColor, vec3(1.0), 0.25), a);
        }`,
    }));
  cone.position.y = GROUND + coneH / 2;
  cone.renderOrder = 2;
  cone.castShadow = cone.receiveShadow = false;
  cone.frustumCulled = false;
  scene.add(cone);

  // ---- 2. dust motes ----
  const N = 300, VOL = { x: 2.8, y: 1.3, z: 2.0, z0: -1.2 };
  const base = new Float32Array(N * 3), seed = new Float32Array(N * 4);
  for (let i = 0; i < N; i++) {
    base[i * 3] = (Math.random() - 0.5) * VOL.x;
    base[i * 3 + 1] = Math.random() * VOL.y;
    base[i * 3 + 2] = VOL.z0 + Math.random() * VOL.z;
    seed[i * 4] = Math.random() * 6.283; seed[i * 4 + 1] = Math.random() * 6.283;
    seed[i * 4 + 2] = 0.5 + Math.random();         // speed factor
    seed[i * 4 + 3] = 0.6 + Math.random() * 0.8;   // size factor
  }
  const dustGeo = new THREE.BufferGeometry();
  dustGeo.setAttribute("position", new THREE.BufferAttribute(base, 3));
  dustGeo.setAttribute("aSeed", new THREE.BufferAttribute(seed, 4));
  const dustU = { uColor: { value: cur.dust.clone() }, uK: { value: 1 }, uTime: { value: 0 }, uPx: { value: 600 }, uMove: { value: 1 }, uConeH: { value: coneH }, uGround: { value: GROUND } };
  const dustGroup = new THREE.Group();
  const dust = new THREE.Points(dustGeo, new THREE.ShaderMaterial({
    uniforms: dustU, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `
      attribute vec4 aSeed;
      uniform float uTime, uPx, uMove, uConeH, uGround;
      varying float vLight; varying float vTw;
      void main() {
        float t = uTime * uMove;
        vec3 p = position;
        p.x += sin(t * 0.21 * aSeed.z + aSeed.x) * 0.12 + sin(t * 0.07 + aSeed.y) * 0.08;
        p.z += cos(t * 0.17 * aSeed.z + aSeed.y) * 0.12;
        // motes rise very slowly and wrap, so the volume never empties
        p.y = mod(p.y + t * 0.012 * aSeed.z + sin(t * 0.3 + aSeed.x) * 0.03, ${VOL.y.toFixed(2)});
        float h = clamp((p.y - uGround) / uConeH, 0.0, 1.0);
        float rad = mix(${CONE_R_BOTTOM.toFixed(3)}, ${CONE_R_TOP.toFixed(3)}, h);
        float d = length(p.xz);
        vLight = 1.0 - smoothstep(rad * 0.7, rad * 1.15, d);
        vTw = 0.75 + 0.25 * sin(t * 0.9 + aSeed.x * 3.0);
        vec4 mv = modelViewMatrix * vec4(p, 1.0);
        gl_PointSize = clamp(0.016 * aSeed.w * uPx / -mv.z, 1.0, 14.0);
        gl_Position = projectionMatrix * mv;
      }`,
    fragmentShader: `
      uniform vec3 uColor; uniform float uK;
      varying float vLight; varying float vTw;
      void main() {
        float r = length(gl_PointCoord - 0.5) * 2.0;
        float s = pow(1.0 - smoothstep(0.0, 1.0, r), 1.6);
        float a = s * (0.10 + 0.34 * vLight) * vTw * uK;
        gl_FragColor = vec4(uColor, a);
      }`,
  }));
  dust.renderOrder = 3; dust.frustumCulled = false; dust.castShadow = dust.receiveShadow = false;
  dustGroup.add(dust);
  scene.add(dustGroup);

  // ---- 3. the floor labyrinth ----
  const FLOOR = 6;
  // Drawn analytically in the shader, in world units, with derivative anti-aliasing: the first
  // version sampled a 512 px canvas stretched over six metres, which at 1080p and above was
  // magnified four to eight times and read as a blurry low-resolution picture.
  const floorU = { uColor: { value: cur.floor.clone() }, uTime: { value: 0 }, uPulse: { value: 1 }, uK: { value: 1 } };
  const floor = new THREE.Mesh(new THREE.PlaneGeometry(FLOOR, FLOOR), new THREE.ShaderMaterial({
    uniforms: floorU, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending,
    vertexShader: `varying vec2 vP; void main() { vP = position.xy; gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0); }`,
    fragmentShader: `
      uniform vec3 uColor; uniform float uTime, uPulse, uK;
      varying vec2 vP;
      const float TAU = 6.2831853;
      // the labyrinth: rings (metres), the angle of each ring's gap, and its width
      const float R[9] = float[9](0.398, 0.656, 0.914, 1.172, 1.43, 1.711, 1.992, 2.297, 2.625);
      const float G[9] = float[9](0.4, 3.3, 1.7, 5.0, 2.5, 0.9, 4.2, 3.0, 5.6);
      // a hairline of constant width on screen, for a coordinate whose lines fall on integers
      float hair(vec2 c, float px) {
        vec2 g = abs(fract(c - 0.5) - 0.5) / max(fwidth(c), 1e-5);
        return 1.0 - clamp(min(g.x, g.y) - px * 0.5 + 0.5, 0.0, 1.0);
      }
      // a wall of constant width in metres, softened by one pixel
      float wall(float d, float w) { float f = max(fwidth(d), 1e-5); return 1.0 - smoothstep(w - f, w + f, abs(d)); }
      void main() {
        float r = length(vP);
        float th = atan(vP.y, vP.x); if (th < 0.0) th += TAU;
        float fade = 1.0 - smoothstep(0.9, 2.95, r);
        float grid = hair(vP / 0.125, 1.0) * 0.09 + hair(vP / 0.5, 1.3) * 0.13;
        float maze = 0.0;
        for (int i = 0; i < 9; i++) {
          float gw = 0.55 + float(i - (i / 3) * 3) * 0.12;
          float inArc = step(gw, mod(th - G[i], TAU));
          maze = max(maze, wall(r - R[i], 0.011) * inArc);
          if (i > 0) {
            int n = 1 + i - (i / 3) * 3;
            float a0 = G[i] + 3.0 + float(i) * 0.9;
            for (int k = 0; k < 3; k++) {
              if (k >= n) break;
              float an = a0 + float(k) * TAU / float(n);
              vec2 dir = vec2(cos(an), sin(an));
              float along = dot(vP, dir), perp = dot(vP, vec2(-dir.y, dir.x));
              float inside = step(R[i - 1], along) * step(along, R[i]);
              maze = max(maze, wall(perp, 0.011) * inside);
            }
          }
        }
        float line = max(grid, maze * 0.75);
        // two rings of light travel outward along the walls; the second is slower and wider
        float q = r / 3.0;
        float w1 = pow(max(0.0, sin((q * 2.2 - uTime * 0.16) * TAU)), 10.0);
        float w2 = pow(max(0.0, sin((q * 1.3 - uTime * 0.09 + 0.3) * TAU)), 6.0) * 0.6;
        float pulse = (w1 + w2) * uPulse;
        float a = line * fade * (0.20 + 0.75 * pulse) * uK;
        // a soft lift under the plinth so the stage reads as a place
        a += (1.0 - smoothstep(0.0, 0.54, r)) * 0.05 * uK;
        gl_FragColor = vec4(uColor, a);
      }`,
  }));
  floor.rotation.x = -Math.PI / 2; floor.position.y = 0.001;
  floor.renderOrder = -2; floor.castShadow = floor.receiveShadow = false;
  scene.add(floor);

  // ---- 4. far app windows ----
  const screens = new THREE.Group();
  scene.add(screens);
  const rrect = (g, x, y, w, h, r) => { g.beginPath(); g.moveTo(x + r, y); g.arcTo(x + w, y, x + w, y + h, r); g.arcTo(x + w, y + h, x, y + h, r); g.arcTo(x, y + h, x, y, r); g.arcTo(x, y, x + w, y, r); g.closePath(); };
  function uiTexture(kind) {
    // drawn at twice the layout size (512 x 320) so the windows stay crisp on large screens
    const W = 256, H = 160, X = 2, c = document.createElement("canvas"); c.width = W * X; c.height = H * X;
    const g = c.getContext("2d"); g.scale(X, X);
    const mint = (a) => `rgba(120,235,208,${a})`;
    g.fillStyle = mint(0.07); rrect(g, 6, 6, W - 12, H - 12, 14); g.fill();
    g.strokeStyle = mint(0.5); g.lineWidth = 2; rrect(g, 6, 6, W - 12, H - 12, 14); g.stroke();
    g.fillStyle = mint(0.35); g.fillRect(8, 26, W - 16, 2);                         // title bar rule
    [0, 1, 2].forEach((i) => { g.beginPath(); g.arc(22 + i * 12, 17, 3, 0, 6.283); g.fill(); });
    if (kind === 0) {            // chat: alternating bubbles
      [[22, 40, 110, 20, 0.5], [118, 68, 112, 20, 0.8], [22, 96, 150, 20, 0.5], [90, 124, 140, 16, 0.8]].forEach(([x, y, w, h, a]) => { g.fillStyle = mint(a * 0.6); rrect(g, x, y, w, h, 9); g.fill(); });
    } else if (kind === 1) {     // bars: a small chart
      for (let i = 0; i < 9; i++) { const h = 20 + ((i * 37) % 70); g.fillStyle = mint(0.35 + (i % 3) * 0.15); g.fillRect(24 + i * 24, 140 - h, 14, h); }
    } else if (kind === 2) {     // list: rows with a dot and a line
      for (let i = 0; i < 5; i++) { g.fillStyle = mint(0.7); g.beginPath(); g.arc(28, 48 + i * 22, 5, 0, 6.283); g.fill(); g.fillStyle = mint(0.35); rrect(g, 42, 43 + i * 22, 70 + ((i * 53) % 110), 10, 5); g.fill(); }
    } else {                     // sidebar and a card
      g.fillStyle = mint(0.25); g.fillRect(14, 34, 50, H - 48);
      g.fillStyle = mint(0.4); rrect(g, 76, 40, 160, 34, 8); g.fill();
      g.fillStyle = mint(0.25); rrect(g, 76, 84, 76, 50, 8); g.fill(); rrect(g, 160, 84, 76, 50, 8); g.fill();
    }
    // the screens are meant to be out of focus; blurring once in the texture is cheaper than any depth-of-field
    const out = document.createElement("canvas"); out.width = W * X; out.height = H * X;
    const o = out.getContext("2d");
    try { o.filter = "blur(1.4px)"; } catch (e) { /* unblurred is acceptable */ }
    o.drawImage(c, 0, 0);
    const t = new THREE.CanvasTexture(out);
    t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 4;
    return t;
  }
  const SCREENS = [
    { x: -2.3, y: 0.95, z: -4.2, w: 1.15, s: 0.20, kind: 0, ry: 0.28, ph: 0.0, op: 0.36 },
    { x: -0.9, y: 1.45, z: -4.9, w: 1.0,  s: 0.20, kind: 1, ry: 0.12, ph: 1.7, op: 0.30 },
    { x: 1.1,  y: 0.55, z: -3.6, w: 1.0,  s: 0.20, kind: 2, ry: -0.18, ph: 3.1, op: 0.38 },
    { x: 2.5,  y: 1.25, z: -4.6, w: 1.25, s: 0.20, kind: 3, ry: -0.30, ph: 4.4, op: 0.30 },
  ];
  const screenMeshes = SCREENS.map((d) => {
    const mat = new THREE.MeshBasicMaterial({ map: uiTexture(d.kind), transparent: true, opacity: d.op, depthWrite: false, blending: THREE.AdditiveBlending, toneMapped: false, color: 0xffffff });
    const m = new THREE.Mesh(new THREE.PlaneGeometry(d.w, d.w * 0.625), mat);
    m.position.set(d.x, d.y, d.z); m.rotation.y = d.ry;
    m.renderOrder = -3; m.castShadow = m.receiveShadow = false;
    m.userData = d;
    screens.add(m);
    return m;
  });
  const screenTint = new THREE.Color(1, 1, 1);

  // ---- state ----
  const ptr = { x: 0, y: 0, sx: 0, sy: 0 };
  let tNow = 0, motion = 0;   // motion: 1 animated, 0 still (reduced); a clock that stops, not a switch

  function applyColors() {
    coneU.uColor.value.copy(cur.cone); coneU.uK.value = cur.coneK;
    dustU.uColor.value.copy(cur.dust); dustU.uK.value = cur.dustK;
    floorU.uColor.value.copy(cur.floor); floorU.uK.value = 0.85 + 0.15 * cur.coneK;
    // the far windows borrow a quarter of the accent: enough to feel the mood, not enough to shout
    screenTint.copy(cur.dust).lerp(new THREE.Color(0xffffff), 0.6);
    for (const m of screenMeshes) m.material.color.copy(screenTint);
  }
  applyColors();

  function setAccent(id) {
    const a = ACCENTS[id] || ACCENTS.welcome;
    to.cone.set(a.cone); to.dust.set(a.dust); to.floor.set(a.floor); to.coneK = a.coneK; to.dustK = a.dustK;
    from.cone.copy(cur.cone); from.dust.copy(cur.dust); from.floor.copy(cur.floor); from.coneK = cur.coneK; from.dustK = cur.dustK;
    if (isReduced()) { cur.cone.copy(to.cone); cur.dust.copy(to.dust); cur.floor.copy(to.floor); cur.coneK = to.coneK; cur.dustK = to.dustK; prog = 1; applyColors(); }
    else prog = 0;
  }

  function pointer(nx, ny) { ptr.x = Math.max(-1, Math.min(1, +nx || 0)); ptr.y = Math.max(-1, Math.min(1, +ny || 0)); }

  function update(dt, t) {
    const still = isReduced();
    if (!still) tNow += dt;                      // the shader clock stops with reduced motion
    const clock = still ? 0 : tNow;
    if (prog < 1) {
      prog = Math.min(1, prog + dt / ACCENT_TIME);
      const k = smooth(prog);
      cur.cone.copy(from.cone).lerp(to.cone, k); cur.dust.copy(from.dust).lerp(to.dust, k); cur.floor.copy(from.floor).lerp(to.floor, k);
      cur.coneK = from.coneK + (to.coneK - from.coneK) * k; cur.dustK = from.dustK + (to.dustK - from.dustK) * k;
      applyColors();
    }
    coneU.uTime.value = clock; coneU.uShimmer.value = still ? 0 : 1;
    dustU.uTime.value = clock + 40; dustU.uMove.value = still ? 0 : 1;
    floorU.uTime.value = still ? 0.37 : clock; floorU.uPulse.value = still ? 0 : 1;
    // point size follows the drawing buffer so the motes keep the same angular size at any dpr
    const { H } = stage.size;
    dustU.uPx.value = (H * stage.renderer.getPixelRatio()) / (2 * Math.tan(THREE.MathUtils.degToRad(stage.camera.fov / 2)));

    // parallax: the far layers lean a few centimetres against the pointer, eased
    const a = still ? 1 : 1 - Math.exp(-3 * dt);
    ptr.sx += (ptr.x - ptr.sx) * a; ptr.sy += (ptr.y - ptr.sy) * a;
    dustGroup.position.set(-ptr.sx * 0.05, -ptr.sy * 0.03, 0);
    screens.position.set(-ptr.sx * 0.10, -ptr.sy * 0.05, 0);
    for (const m of screenMeshes) {
      const d = m.userData;
      m.position.y = d.y + (still ? 0 : Math.sin(clock * 0.18 + d.ph) * 0.04);
      m.position.x = d.x + (still ? 0 : Math.sin(clock * 0.11 + d.ph * 1.7) * 0.03);
    }
  }

  return { setAccent, update, pointer };
}
