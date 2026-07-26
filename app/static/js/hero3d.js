/* Three.js hero scene.
 *
 * A conversation rendered as terrain. Time runs left to right, amplitude is
 * height, and the layers receding into depth are the same signal offset in
 * phase — enough parallax to read as a landscape rather than a chart.
 *
 * The point of doing it in 3D is the canyon. In the middle of the field the
 * bars stop entirely, leaving a void you can look *into* and see the far wall
 * of. That gap is `voice.turn.time_to_first_audio` — the silence the user sat
 * through, and the thing no request/response trace records. Making it a hole
 * in the terrain says that faster than the copy next to it does.
 *
 * Geometry is computed once and only instance colours animate, so the scene
 * costs almost nothing per frame.
 */

import * as THREE from "/static/vendor/three.module.min.js";

const CYCLE = 9200; // ms, matches the console's replay story

/* Two turns, deliberately contrasted: the first reply is degraded (1180ms of
 * dead air, well past the point a listener assumes they were not heard), the
 * second is healthy (340ms). Showing both teaches the metric — one wide canyon
 * and one narrow one says more than either alone. Both values are realistic. */
const SEGMENTS = [
  { from: 0,    to: 1800, kind: "user" },
  { from: 1800, to: 2980, kind: "silence" },  // 1180ms — degraded
  { from: 2980, to: 5000, kind: "agent" },
  { from: 5000, to: 6200, kind: "user" },     // barge-in at 5000
  { from: 6200, to: 6540, kind: "silence" },  // 340ms — healthy
  { from: 6540, to: 9200, kind: "agent" },
];
const BARGE_AT = 5000;

const COLOURS = {
  user: new THREE.Color(0x38bdf8),
  agent: new THREE.Color(0xa78bfa),
};
const AMBER = new THREE.Color(0xfbbf24);
const ROSE = new THREE.Color(0xfb7185);

// Sparse enough that the terrain reads as individual samples with air between
// them. Denser than this and it fuses into a solid wall, which hides the very
// thing the scene exists to show.
const NX = 96;    // bars along time
const NZ = 11;    // depth layers
const SPAN_X = 11;
const SPAN_Z = 3.4;

export function mountHero(canvas) {
  const renderer = new THREE.WebGLRenderer({
    canvas,
    antialias: true,
    alpha: true,
    powerPreference: "high-performance",
  });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));

  const scene = new THREE.Scene();
  // Light fog only — enough to separate the depth layers, not enough to crush
  // them. Anything denser and the terrain reads as a dark smear.
  scene.fog = new THREE.FogExp2(0x05070a, 0.052);

  // Raised and angled down: you have to look *into* the canyon for it to read
  // as a gap rather than a seam.
  const camera = new THREE.PerspectiveCamera(40, 1, 0.1, 100);
  camera.position.set(0.4, 3.5, 6.2);
  camera.lookAt(0, 0.05, 0);

  const world = new THREE.Group();
  world.rotation.y = -0.24;
  scene.add(world);

  // ── the terrain ────────────────────────────────────────────────
  const geometry = new THREE.BoxGeometry(1, 1, 1);
  // No `vertexColors` here: that flag makes the shader read a `color`
  // attribute off the geometry, which BoxGeometry does not have, and every
  // bar renders black. three.js wires up `instanceColor` from setColorAt on
  // its own.
  const material = new THREE.MeshBasicMaterial({
    transparent: true,
    opacity: 0.95,
  });

  const barW = (SPAN_X / NX) * 0.55;
  const barD = (SPAN_Z / NZ) * 0.42;

  // Precompute which bars exist and how tall they are.
  const cells = [];
  for (let ix = 0; ix < NX; ix++) {
    const t = (ix / NX) * CYCLE;
    const seg = SEGMENTS.find((s) => t >= s.from && t < s.to);
    if (!seg || seg.kind === "silence") continue;
    for (let iz = 0; iz < NZ; iz++) {
      // Phase-shift each depth layer so the layers do not read as one extruded
      // slab.
      const phase = iz * 41;
      const h = envelope(seg.kind, t + phase, ix * 7.3 + iz);
      cells.push({ ix, iz, t, kind: seg.kind, h });
    }
  }

  const mesh = new THREE.InstancedMesh(geometry, material, cells.length);
  mesh.instanceMatrix.setUsage(THREE.StaticDrawUsage);

  const dummy = new THREE.Object3D();
  cells.forEach((cell, i) => {
    const x = (cell.ix / (NX - 1) - 0.5) * SPAN_X;
    const z = (cell.iz / (NZ - 1) - 0.5) * SPAN_Z;
    dummy.position.set(x, cell.h / 2, z);
    dummy.scale.set(barW, cell.h, barD);
    dummy.updateMatrix();
    mesh.setMatrixAt(i, dummy.matrix);
    mesh.setColorAt(i, COLOURS[cell.kind]);
  });
  mesh.instanceMatrix.needsUpdate = true;
  world.add(mesh);

  // Mirrored copy below the floor: a cheap reflection that makes the terrain
  // feel like it is floating rather than sitting on nothing.
  const reflection = new THREE.InstancedMesh(
    geometry,
    new THREE.MeshBasicMaterial({
      transparent: true,
      opacity: 0.16,
      depthWrite: false,
    }),
    cells.length
  );
  cells.forEach((cell, i) => {
    const x = (cell.ix / (NX - 1) - 0.5) * SPAN_X;
    const z = (cell.iz / (NZ - 1) - 0.5) * SPAN_Z;
    dummy.position.set(x, -cell.h / 2 - 0.04, z);
    dummy.scale.set(barW, cell.h, barD);
    dummy.updateMatrix();
    reflection.setMatrixAt(i, dummy.matrix);
    reflection.setColorAt(i, COLOURS[cell.kind]);
  });
  reflection.instanceMatrix.needsUpdate = true;
  world.add(reflection);

  // ── the canyons ────────────────────────────────────────────────
  // Each silence gets an amber floor and two walls, so the void is legible as
  // a measured interval rather than an absence of geometry.
  for (const seg of SEGMENTS) {
    if (seg.kind !== "silence") continue;
    const x0 = (seg.from / CYCLE - 0.5) * SPAN_X;
    const x1 = (seg.to / CYCLE - 0.5) * SPAN_X;

    const floor = new THREE.Mesh(
      new THREE.PlaneGeometry(x1 - x0, SPAN_Z * 1.15),
      new THREE.MeshBasicMaterial({
        color: AMBER, transparent: true, opacity: 0.3,
        side: THREE.DoubleSide, depthWrite: false,
        blending: THREE.AdditiveBlending,
      })
    );
    floor.rotation.x = -Math.PI / 2;
    floor.position.set((x0 + x1) / 2, 0.005, 0);
    world.add(floor);

    for (const x of [x0, x1]) {
      const wall = new THREE.Mesh(
        new THREE.PlaneGeometry(SPAN_Z * 1.15, 1.5),
        new THREE.MeshBasicMaterial({
          color: AMBER, transparent: true, opacity: 0.22,
          side: THREE.DoubleSide, depthWrite: false,
          blending: THREE.AdditiveBlending,
        })
      );
      wall.rotation.y = Math.PI / 2;
      wall.position.set(x, 0.75, 0);
      world.add(wall);
    }
  }

  // ── barge-in marker ────────────────────────────────────────────
  const bargeX = (BARGE_AT / CYCLE - 0.5) * SPAN_X;
  const barge = new THREE.Mesh(
    new THREE.PlaneGeometry(SPAN_Z * 1.2, 2.4),
    new THREE.MeshBasicMaterial({
      color: ROSE, transparent: true, opacity: 0.5,
      side: THREE.DoubleSide, depthWrite: false,
    })
  );
  barge.rotation.y = Math.PI / 2;
  barge.position.set(bargeX, 1.2, 0);
  world.add(barge);

  // ── playhead ───────────────────────────────────────────────────
  const playhead = new THREE.Mesh(
    new THREE.PlaneGeometry(SPAN_Z * 1.3, 2.8),
    new THREE.MeshBasicMaterial({
      color: 0xffffff, transparent: true, opacity: 0.16,
      side: THREE.DoubleSide, depthWrite: false,
    })
  );
  playhead.rotation.y = Math.PI / 2;
  world.add(playhead);

  // ── interaction + loop ─────────────────────────────────────────
  const pointer = { x: 0, y: 0, tx: 0, ty: 0 };
  function onPointerMove(event) {
    const rect = canvas.getBoundingClientRect();
    pointer.tx = ((event.clientX - rect.left) / rect.width - 0.5) * 2;
    pointer.ty = ((event.clientY - rect.top) / rect.height - 0.5) * 2;
  }
  window.addEventListener("pointermove", onPointerMove, { passive: true });

  function resize() {
    const rect = canvas.getBoundingClientRect();
    if (!rect.width || !rect.height) return;
    renderer.setSize(rect.width, rect.height, false);
    camera.aspect = rect.width / rect.height;
    camera.updateProjectionMatrix();
  }
  resize();
  const observer = new ResizeObserver(resize);
  observer.observe(canvas);

  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const colour = new THREE.Color();
  let raf = 0;

  function frame(now) {
    const elapsed = reduceMotion ? CYCLE * 0.62 : now % CYCLE;
    const headT = elapsed;
    playhead.position.set((headT / CYCLE - 0.5) * SPAN_X, 1.4, 0);

    // Bars behind the playhead burn bright; ahead of it they sit dark, so the
    // conversation reads as being drawn in real time.
    for (let i = 0; i < cells.length; i++) {
      const cell = cells[i];
      const lead = headT - cell.t;
      let intensity;
      // MeshBasicMaterial multiplies the instance colour, so anything below 1
      // darkens. Unplayed bars still need to be clearly visible — they are the
      // shape of the conversation, not absence.
      if (lead < 0) intensity = 0.5;
      else if (lead < 260) intensity = 2.2;          // flare at the head
      else intensity = 1.05 + 0.55 * Math.exp(-lead / 2600);
      colour.copy(COLOURS[cell.kind]).multiplyScalar(intensity);
      mesh.setColorAt(i, colour);
      reflection.setColorAt(i, colour);
    }
    mesh.instanceColor.needsUpdate = true;
    reflection.instanceColor.needsUpdate = true;

    barge.material.opacity =
      headT >= BARGE_AT ? 0.34 + Math.sin(now / 220) * 0.16 : 0.06;

    if (!reduceMotion) {
      pointer.x += (pointer.tx - pointer.x) * 0.045;
      pointer.y += (pointer.ty - pointer.y) * 0.045;
      world.rotation.y = -0.19 + pointer.x * 0.14 + Math.sin(now / 7000) * 0.045;
      world.rotation.x = pointer.y * 0.05;
      camera.position.y = 2.35 - pointer.y * 0.28;
      camera.lookAt(0, 0.25, 0);
    }

    renderer.render(scene, camera);
    raf = requestAnimationFrame(frame);
  }
  raf = requestAnimationFrame(frame);

  return () => {
    cancelAnimationFrame(raf);
    observer.disconnect();
    window.removeEventListener("pointermove", onPointerMove);
    renderer.dispose();
    geometry.dispose();
  };
}

/* Deterministic pseudo-noise: Math.random() per bar would reshuffle the
 * terrain on every reload, and the shape is part of the story. */
function noise(seed) {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

function envelope(kind, t, seed) {
  // Kept low relative to the bar spacing so the result reads as terrain seen
  // from above rather than a picket fence seen from the side.
  const base = kind === "user" ? 0.16 : 0.13;
  const swing = kind === "user" ? 0.42 : 0.36;
  const period = kind === "user" ? 168 : 137;
  return (
    base +
    Math.abs(Math.sin(t / period)) * swing +
    Math.abs(Math.sin(t / 47)) * 0.1 +
    noise(seed) * 0.12
  );
}
