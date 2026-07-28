/*
 * URTO home scene — full-viewport WebGL space environment.
 * Three.js, vendored under /static/vendor/three (no CDN dependency).
 */

import * as THREE from 'three';
import { EffectComposer } from 'three/addons/postprocessing/EffectComposer.js';
import { RenderPass } from 'three/addons/postprocessing/RenderPass.js';
import { UnrealBloomPass } from 'three/addons/postprocessing/UnrealBloomPass.js';
import { OutputPass } from 'three/addons/postprocessing/OutputPass.js';
import { FontLoader } from 'three/addons/loaders/FontLoader.js';
import { TextGeometry } from 'three/addons/geometries/TextGeometry.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';

const stage = document.getElementById('spaceStage');
const captionEl = document.getElementById('spaceCaption');
const fadeEl = document.getElementById('spaceFade');

let renderer, scene, camera, composer;
let active = false;
let rafId = null;
const clock = new THREE.Clock();
let elapsed = 0;

const mouse = { x: 0, y: 0 };           // normalized -1..1
const raycaster = new THREE.Raycaster();
const pointerNdc = new THREE.Vector2();
let pointerDirty = false;

const starLayers = [];
const nebulas = [];
const rings = [];
const orbs = [];
let logoGroup = null;
let logoIntroT = 0;
let introCamT = 0;
let hoveredOrb = null;
let flight = null; // { from, to, t } camera fly-in on orb click

const ORB_DEFS = [
  { key: 'lookup', label: 'SKIP TRACE', caption: 'Enter URTO Skip Trace →', color: 0x38d9ff, radius: 17.5, speed: 0.12, phase: 0.0, tiltX: 0.42, tiltZ: 0.10, bobSpeed: 1.1 },
  { key: 'crm', label: 'CRM', caption: 'Enter URTO CRM →', color: 0xa78bfa, radius: 21.5, speed: -0.09, phase: 2.1, tiltX: -0.30, tiltZ: 0.24, bobSpeed: 0.8 },
  { key: 'dialer', label: 'DIALER', caption: 'Enter URTO Dialer →', color: 0xf472b6, radius: 25.5, speed: 0.075, phase: 4.2, tiltX: 0.16, tiltZ: -0.34, bobSpeed: 0.95 },
  { key: 'txn', label: 'TRANSACTIONS', caption: 'Enter Transaction Manager →', color: 0xf5c451, radius: 29.5, speed: -0.06, phase: 5.6, tiltX: 0.08, tiltZ: 0.30, bobSpeed: 0.7 },
];

function makeRadialTexture(inner, outer) {
  const c = document.createElement('canvas');
  c.width = c.height = 256;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(128, 128, 0, 128, 128, 128);
  g.addColorStop(0, inner);
  g.addColorStop(1, outer);
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, 256, 256);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function makeLabelTexture(text, accentCss) {
  const c = document.createElement('canvas');
  c.width = 1024;
  c.height = 192;
  const ctx = c.getContext('2d');
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  const letterSpacingPx = 18;
  try { ctx.letterSpacing = letterSpacingPx + 'px'; } catch (e) { /* older engines */ }

  // Auto-shrink so a longer label (e.g. "TRANSACTIONS") still fits the same
  // canvas width as short ones ("CRM") instead of overflowing/clipping.
  let fontSize = 84;
  const maxWidth = 920;
  ctx.font = `700 ${fontSize}px "Segoe UI", -apple-system, Roboto, sans-serif`;
  const measuredWidth = () => ctx.measureText(text).width + letterSpacingPx * Math.max(text.length - 1, 0);
  while (fontSize > 34 && measuredWidth() > maxWidth) {
    fontSize -= 4;
    ctx.font = `700 ${fontSize}px "Segoe UI", -apple-system, Roboto, sans-serif`;
  }

  ctx.shadowColor = accentCss;
  ctx.shadowBlur = 42;
  ctx.fillStyle = 'rgba(255,255,255,0.96)';
  ctx.fillText(text, 512, 100);
  ctx.shadowBlur = 0;
  ctx.fillText(text, 512, 100);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function buildStars() {
  const starTex = makeRadialTexture('rgba(255,255,255,1)', 'rgba(255,255,255,0)');
  const layers = [
    { count: 1400, size: 0.5, color: 0xffffff, opacity: 0.8, rot: 0.004 },
    { count: 900, size: 0.8, color: 0x9db4ff, opacity: 0.6, rot: -0.006 },
    { count: 500, size: 1.15, color: 0xffd9ec, opacity: 0.45, rot: 0.009 },
  ];
  for (const def of layers) {
    const positions = new Float32Array(def.count * 3);
    for (let i = 0; i < def.count; i++) {
      // random point on a shell so stars surround the camera at depth
      const r = 70 + Math.random() * 110;
      const theta = Math.random() * Math.PI * 2;
      const phi = Math.acos(2 * Math.random() - 1);
      positions[i * 3] = r * Math.sin(phi) * Math.cos(theta);
      positions[i * 3 + 1] = r * Math.sin(phi) * Math.sin(theta) * 0.72;
      positions[i * 3 + 2] = r * Math.cos(phi);
    }
    const geo = new THREE.BufferGeometry();
    geo.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    const mat = new THREE.PointsMaterial({
      size: def.size,
      color: def.color,
      map: starTex,
      transparent: true,
      opacity: def.opacity,
      depthWrite: false,
      sizeAttenuation: true,
      blending: THREE.AdditiveBlending,
    });
    const points = new THREE.Points(geo, mat);
    scene.add(points);
    starLayers.push({ points, mat, baseOpacity: def.opacity, rot: def.rot, twinklePhase: Math.random() * 6 });
  }
}

function buildNebulas() {
  const palettes = [
    ['rgba(99,102,241,0.55)', 'rgba(99,102,241,0)'],
    ['rgba(34,211,238,0.4)', 'rgba(34,211,238,0)'],
    ['rgba(217,70,139,0.4)', 'rgba(217,70,139,0)'],
    ['rgba(59,130,246,0.45)', 'rgba(59,130,246,0)'],
    ['rgba(167,139,250,0.45)', 'rgba(167,139,250,0)'],
  ];
  for (let i = 0; i < 7; i++) {
    const [inner, outer] = palettes[i % palettes.length];
    const mat = new THREE.SpriteMaterial({
      map: makeRadialTexture(inner, outer),
      blending: THREE.AdditiveBlending,
      transparent: true,
      opacity: 0.035 + Math.random() * 0.03,
      depthWrite: false,
      rotation: Math.random() * Math.PI,
    });
    const sprite = new THREE.Sprite(mat);
    const s = 110 + Math.random() * 130;
    sprite.scale.set(s, s, 1);
    const ang = Math.random() * Math.PI * 2;
    const dist = 35 + Math.random() * 70; // keep the center clear for the logo
    sprite.position.set(Math.cos(ang) * dist, Math.sin(ang) * dist * 0.55, -55 - Math.random() * 60);
    scene.add(sprite);
    nebulas.push({ sprite, mat, drift: 0.02 + Math.random() * 0.05, phase: Math.random() * 6, baseX: sprite.position.x, baseY: sprite.position.y });
  }
}

function buildRings() {
  const defs = [
    { r: 14, tube: 0.05, color: 0x4a6cff, opacity: 0.19, tx: 0.5, tz: 0.1, wobble: 0.05, ws: 0.13 },
    { r: 18.5, tube: 0.045, color: 0x8a5cff, opacity: 0.15, tx: -0.35, tz: 0.28, wobble: 0.07, ws: 0.09 },
    { r: 23.5, tube: 0.04, color: 0x2fd4ff, opacity: 0.11, tx: 0.18, tz: -0.4, wobble: 0.06, ws: 0.07 },
  ];
  for (const d of defs) {
    const mesh = new THREE.Mesh(
      new THREE.TorusGeometry(d.r, d.tube, 8, 160),
      new THREE.MeshBasicMaterial({ color: d.color, transparent: true, opacity: d.opacity })
    );
    mesh.rotation.set(Math.PI / 2 + d.tx, 0, d.tz);
    scene.add(mesh);
    rings.push({ mesh, baseTx: Math.PI / 2 + d.tx, wobble: d.wobble, ws: d.ws, phase: Math.random() * 6 });
  }
}

function buildOrbs() {
  for (const def of ORB_DEFS) {
    const orbitGroup = new THREE.Group();
    orbitGroup.rotation.x = def.tiltX;
    orbitGroup.rotation.z = def.tiltZ;
    scene.add(orbitGroup);

    const holder = new THREE.Group(); // positioned on the orbit each frame
    orbitGroup.add(holder);

    const colorCss = '#' + def.color.toString(16).padStart(6, '0');

    const core = new THREE.Mesh(
      new THREE.SphereGeometry(1.35, 48, 48),
      new THREE.MeshStandardMaterial({
        color: new THREE.Color(def.color).multiplyScalar(0.35),
        emissive: def.color,
        emissiveIntensity: 0.85,
        roughness: 0.35,
        metalness: 0.1,
      })
    );
    holder.add(core);

    const glow = new THREE.Sprite(new THREE.SpriteMaterial({
      map: makeRadialTexture('rgba(255,255,255,0.65)', 'rgba(255,255,255,0)'),
      color: def.color,
      blending: THREE.AdditiveBlending,
      transparent: true,
      opacity: 0.22,
      depthWrite: false,
    }));
    glow.scale.set(6.5, 6.5, 1);
    holder.add(glow);

    const label = new THREE.Sprite(new THREE.SpriteMaterial({
      map: makeLabelTexture(def.label, colorCss),
      transparent: true,
      depthWrite: false,
    }));
    label.scale.set(9.2, 1.72, 1);
    label.position.y = -3.3;
    holder.add(label);

    const hit = new THREE.Mesh(
      new THREE.SphereGeometry(4.6, 12, 12),
      new THREE.MeshBasicMaterial({ visible: false })
    );
    hit.userData.orbKey = def.key;
    holder.add(hit);

    const light = new THREE.PointLight(def.color, 55, 55, 2);
    holder.add(light);

    orbs.push({ def, orbitGroup, holder, core, glow, label, hit, light, hoverT: 0 });
  }
}

function buildLogo() {
  const loader = new FontLoader();
  loader.load('/static/fonts/helvetiker_bold.typeface.json', (font) => {
    const geo = new TextGeometry('URTO', {
      font,
      size: 6.7,
      depth: 2.4,
      curveSegments: 12,
      bevelEnabled: true,
      bevelThickness: 0.5,
      bevelSize: 0.3,
      bevelSegments: 5,
    });
    geo.center();

    const mat = new THREE.MeshPhysicalMaterial({
      color: 0xc9d6ff,
      metalness: 1.0,
      roughness: 0.16,
      clearcoat: 1.0,
      clearcoatRoughness: 0.12,
      emissive: 0x0a1a4a,
      emissiveIntensity: 0.3,
      envMapIntensity: 0.85,
    });

    logoGroup = new THREE.Group();
    const mesh = new THREE.Mesh(geo, mat);
    logoGroup.add(mesh);
    logoGroup.scale.setScalar(0.001);
    scene.add(logoGroup);
  });
}

function init() {
  renderer = new THREE.WebGLRenderer({ antialias: true, powerPreference: 'high-performance' });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(window.innerWidth, window.innerHeight);
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.12;
  stage.appendChild(renderer.domElement);

  scene = new THREE.Scene();
  scene.background = new THREE.Color(0x04060d);

  camera = new THREE.PerspectiveCamera(55, window.innerWidth / window.innerHeight, 0.1, 500);
  camera.position.set(0, 0, 108);

  // Image-based lighting so the metal logo has something real to reflect.
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(renderer), 0.04).texture;

  const key = new THREE.DirectionalLight(0xbfd0ff, 1.6);
  key.position.set(6, 10, 14);
  scene.add(key);
  const rimCyan = new THREE.DirectionalLight(0x2fd4ff, 2.2);
  rimCyan.position.set(-14, 3, -6);
  scene.add(rimCyan);
  const rimMagenta = new THREE.DirectionalLight(0xf25ab3, 1.7);
  rimMagenta.position.set(13, -7, -4);
  scene.add(rimMagenta);
  scene.add(new THREE.AmbientLight(0x111a33, 1.2));

  buildStars();
  buildNebulas();
  buildRings();
  buildOrbs();
  buildLogo();

  composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.32, 0.4, 0.66);
  composer.addPass(bloom);
  composer.addPass(new OutputPass());

  window.addEventListener('resize', onResize);
  window.addEventListener('pointermove', onPointerMove, { passive: true });
  stage.addEventListener('click', onClick);
}

function onResize() {
  camera.aspect = window.innerWidth / window.innerHeight;
  camera.updateProjectionMatrix();
  renderer.setSize(window.innerWidth, window.innerHeight);
  composer.setSize(window.innerWidth, window.innerHeight);
}

function onPointerMove(e) {
  mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
  mouse.y = (e.clientY / window.innerHeight) * 2 - 1;
  pointerNdc.set(mouse.x, -mouse.y);
  pointerDirty = true;
}

function onClick(e) {
  if (flight) return;
  // Fresh raycast at the click point — the orbs keep moving, so the cached
  // hover can be stale if the pointer wasn't moving at the moment of click.
  if (typeof e?.clientX === 'number') {
    pointerNdc.set((e.clientX / window.innerWidth) * 2 - 1, -((e.clientY / window.innerHeight) * 2 - 1));
    raycaster.setFromCamera(pointerNdc, camera);
    const hits = raycaster.intersectObjects(orbs.map(o => o.hit), false);
    hoveredOrb = hits.length ? orbs.find(o => o.hit === hits[0].object) : null;
  }
  if (!hoveredOrb) return;
  const target = new THREE.Vector3();
  hoveredOrb.core.getWorldPosition(target);
  flight = {
    key: hoveredOrb.def.key,
    from: camera.position.clone(),
    to: target.clone().add(target.clone().normalize().multiplyScalar(-6)),
    look: target.clone(),
    t: 0,
  };
  fadeEl.style.opacity = '1';
}

function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }
function easeOutBack(t) {
  const c1 = 1.70158, c3 = c1 + 1;
  return 1 + c3 * Math.pow(t - 1, 3) + c1 * Math.pow(t - 1, 2);
}

function updateHover() {
  if (!pointerDirty) return;
  pointerDirty = false;
  raycaster.setFromCamera(pointerNdc, camera);
  const hits = raycaster.intersectObjects(orbs.map(o => o.hit), false);
  const next = hits.length ? orbs.find(o => o.hit === hits[0].object) : null;
  if (next !== hoveredOrb) {
    hoveredOrb = next;
    document.body.style.cursor = hoveredOrb ? 'pointer' : '';
    if (hoveredOrb) {
      captionEl.textContent = hoveredOrb.def.caption;
      captionEl.style.opacity = '1';
    } else {
      captionEl.style.opacity = '0';
    }
  }
}

function tick() {
  rafId = null;
  if (!active) return;
  schedule();

  const dt = Math.min(clock.getDelta(), 0.05);
  elapsed += dt;
  const t = elapsed;

  // Camera: intro fly-in, then mouse parallax around the resting point.
  if (flight) {
    flight.t += dt / 0.7;
    const k = easeOutCubic(Math.min(flight.t, 1));
    camera.position.lerpVectors(flight.from, flight.to, k);
    camera.lookAt(flight.look);
    if (flight.t >= 1) {
      const key = flight.key;
      flight = null;
      camera.position.set(0, 0, 34);
      introCamT = 1;
      if (window.__urtoEnterSection) window.__urtoEnterSection(key);
      // fade resets when the scene is shown again
      setTimeout(() => { fadeEl.style.opacity = '0'; }, 250);
    }
  } else {
    if (introCamT < 1) {
      introCamT = Math.min(introCamT + dt / 2.4, 1);
    }
    const z = 108 + (34 - 108) * easeOutCubic(introCamT);
    camera.position.z += (z - camera.position.z) * 0.5;
    camera.position.x += (mouse.x * 2.4 - camera.position.x) * 0.03;
    camera.position.y += (-mouse.y * 1.7 - camera.position.y) * 0.03;
    camera.lookAt(0, 0, 0);
  }

  // Stars: slow counter-rotations + twinkle.
  for (const layer of starLayers) {
    layer.points.rotation.y += layer.rot * dt;
    layer.points.rotation.x += layer.rot * 0.4 * dt;
    layer.mat.opacity = layer.baseOpacity * (0.82 + 0.18 * Math.sin(t * 1.7 + layer.twinklePhase));
  }

  // Nebulas: drift and slow spin.
  for (const n of nebulas) {
    n.mat.rotation += n.drift * 0.12 * dt;
    n.sprite.position.x = n.baseX + Math.sin(t * n.drift + n.phase) * 6;
    n.sprite.position.y = n.baseY + Math.cos(t * n.drift * 0.8 + n.phase) * 4;
  }

  // Rings: gentle precession.
  for (const r of rings) {
    r.mesh.rotation.x = r.baseTx + Math.sin(t * r.ws + r.phase) * r.wobble;
    r.mesh.rotation.z += 0.02 * dt;
  }

  // Logo: intro pop, then float in 3D space with mouse-follow tilt.
  if (logoGroup) {
    if (logoIntroT < 1) {
      logoIntroT = Math.min(logoIntroT + dt / 1.5, 1);
      logoGroup.scale.setScalar(Math.max(easeOutBack(logoIntroT), 0.001));
    }
    logoGroup.rotation.y = Math.sin(t * 0.22) * 0.26 + mouse.x * 0.14;
    logoGroup.rotation.x = Math.sin(t * 0.16 + 2) * 0.09 + mouse.y * 0.1;
    logoGroup.position.y = Math.sin(t * 0.6) * 0.55;
  }

  // Orbs: tilted orbits + bob + hover response.
  for (const orb of orbs) {
    const d = orb.def;
    const targetHover = orb === hoveredOrb ? 1 : 0;
    orb.hoverT += (targetHover - orb.hoverT) * Math.min(dt * 8, 1);

    // Advance each orb's own orbital clock, but nearly freeze a hovered orb
    // so it becomes an easy, near-stationary click target while still drifting.
    orb.orbitClock = (orb.orbitClock ?? (t * d.speed)) + dt * d.speed * (1 - orb.hoverT * 0.92);
    const a = orb.orbitClock + d.phase;
    orb.holder.position.set(Math.cos(a) * d.radius, Math.sin(t * d.bobSpeed + d.phase) * 0.9, Math.sin(a) * d.radius);

    const s = 1 + orb.hoverT * 0.3;
    orb.core.scale.setScalar(s);
    orb.glow.scale.setScalar(6.5 * (1 + orb.hoverT * 0.45));
    orb.core.material.emissiveIntensity = 0.85 + orb.hoverT * 0.9;
    orb.light.intensity = 55 + orb.hoverT * 75;
  }

  updateHover();
  composer.render();
}

function schedule() {
  if (rafId === null) rafId = requestAnimationFrame(tick);
}

function start() {
  if (!renderer) return;
  if (!active) {
    active = true;
    clock.getDelta(); // flush accumulated time so dt doesn't jump
    schedule();
  }
}

function stop() {
  active = false;
  if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
  document.body.style.cursor = '';
}

try {
  init();
  window.URTO_SPACE = {
    start, stop,
  };
  window.URTO_SPACE_READY = true;
  if (document.body.classList.contains('space-mode')) start();
} catch (err) {
  console.error('URTO space scene failed to start:', err);
  if (window.__urtoSpaceFailed) window.__urtoSpaceFailed();
}
