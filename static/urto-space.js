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
  try { ctx.letterSpacing = '18px'; } catch (e) { /* older engines */ }
  ctx.font = '700 84px "Segoe UI", -apple-system, Roboto, sans-serif';
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
      opacity: 0.06 + Math.random() * 0.05,
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
    { r: 14, tube: 0.05, color: 0x4a6cff, opacity: 0.30, tx: 0.5, tz: 0.1, wobble: 0.05, ws: 0.13 },
    { r: 18.5, tube: 0.045, color: 0x8a5cff, opacity: 0.24, tx: -0.35, tz: 0.28, wobble: 0.07, ws: 0.09 },
    { r: 23.5, tube: 0.04, color: 0x2fd4ff, opacity: 0.18, tx: 0.18, tz: -0.4, wobble: 0.06, ws: 0.07 },
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
        emissiveIntensity: 1.3,
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
      opacity: 0.38,
      depthWrite: false,
    }));
    glow.scale.set(8.5, 8.5, 1);
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

    const light = new THREE.PointLight(def.color, 90, 55, 2);
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

/* ---- Occasional skywriting rocket ---- */
const FLYBY_TEXT = 'Made by Mario Bengochea';
let flyby = null;

// Soft round smoke puff used for the skywriting point-cloud: a hazy sunlit
// vapor look — bright soft core, quick falloff to nothing at the rim.
function makeSmokePointTexture() {
  const s = 128;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0.0, 'rgba(255,255,255,1)');
  g.addColorStop(0.22, 'rgba(238,245,255,0.82)');
  g.addColorStop(0.55, 'rgba(206,222,248,0.30)');
  g.addColorStop(1.0, 'rgba(206,222,248,0)');
  ctx.fillStyle = g;
  ctx.beginPath(); ctx.arc(s / 2, s / 2, s / 2, 0, Math.PI * 2); ctx.fill();
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

// Sample the message into a cloud of points (one per lit pixel on a grid) so
// the rocket can *write* it as drifting smoke rather than wiping a flat label
// into view. Returns typed arrays in a centered coordinate space `worldW` wide,
// with a per-point `reveal` (0..1 left→right) driving the write-on order.
function sampleTextPoints(text, worldW) {
  const cw = 1600, ch = 230;
  const c = document.createElement('canvas');
  c.width = cw; c.height = ch;
  const ctx = c.getContext('2d');
  ctx.clearRect(0, 0, cw, ch);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.fillStyle = '#fff';
  // Italic semibold reads as "written in motion"; Mario's Windows PC renders
  // the Segoe faces cleanly, with graceful fallbacks on other machines.
  ctx.font = 'italic 700 132px "Segoe UI Semibold", "Segoe UI", "Helvetica Neue", Arial, sans-serif';
  try { ctx.letterSpacing = '2px'; } catch (e) { /* older engines */ }
  ctx.fillText(text, cw / 2, ch / 2);

  const data = ctx.getImageData(0, 0, cw, ch).data;
  const step = 6;
  const worldH = worldW * (ch / cw);
  const positions = [], reveals = [], seeds = [];
  for (let py = 0; py < ch; py += step) {
    for (let px = 0; px < cw; px += step) {
      if (data[(py * cw + px) * 4 + 3] < 110) continue;
      const jx = (Math.random() - 0.5) * step;   // jitter off the grid
      const jy = (Math.random() - 0.5) * step;
      const nx = (px + jx) / cw;
      const ny = (py + jy) / ch;
      positions.push((nx - 0.5) * worldW, (0.5 - ny) * worldH, 0);
      reveals.push(nx);
      seeds.push(Math.random());
    }
  }
  return {
    positions: new Float32Array(positions),
    reveals: new Float32Array(reveals),
    seeds: new Float32Array(seeds),
    count: reveals.length,
  };
}

function makePuffTexture() {
  // A softly shaded "lit sphere" look (highlight offset toward the key light,
  // a multiplied shadow crescent opposite it) so the smoke reads as volumetric
  // puffs rather than flat blurred discs.
  const s = 160;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(s * 0.42, s * 0.4, s * 0.04, s * 0.5, s * 0.5, s * 0.5);
  g.addColorStop(0, 'rgba(228,234,246,0.55)');
  g.addColorStop(0.35, 'rgba(192,202,222,0.34)');
  g.addColorStop(0.75, 'rgba(150,162,188,0.15)');
  g.addColorStop(1, 'rgba(150,162,188,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, s, s);
  ctx.globalCompositeOperation = 'multiply';
  const shadow = ctx.createRadialGradient(s * 0.62, s * 0.64, s * 0.02, s * 0.6, s * 0.62, s * 0.4);
  shadow.addColorStop(0, 'rgba(55,64,90,0.28)');
  shadow.addColorStop(1, 'rgba(55,64,90,0)');
  ctx.fillStyle = shadow;
  ctx.beginPath(); ctx.arc(s * 0.6, s * 0.62, s * 0.4, 0, Math.PI * 2); ctx.fill();
  ctx.globalCompositeOperation = 'source-over';
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

function makeSparkTexture() {
  const s = 64;
  const c = document.createElement('canvas');
  c.width = c.height = s;
  const ctx = c.getContext('2d');
  const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
  g.addColorStop(0, 'rgba(255,244,214,1)');
  g.addColorStop(0.4, 'rgba(255,196,110,0.8)');
  g.addColorStop(1, 'rgba(255,140,60,0)');
  ctx.fillStyle = g;
  ctx.fillRect(0, 0, s, s);
  const tex = new THREE.CanvasTexture(c);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

/* Real lit 3D rocket (hull + nose + fins + nozzle) instead of a flat sprite,
 * so it shades under the same key/rim lights as the rest of the scene and
 * holds up next to the metal logo and lit orbs. Built pointing +Y, then
 * rotated -90° about Z so the nose faces +X (the direction of flight). */
function buildRocketGroup() {
  const rocket = new THREE.Group();
  const hull = new THREE.Group();
  hull.rotation.z = -Math.PI / 2;
  rocket.add(hull);

  const bodyMat = new THREE.MeshPhysicalMaterial({
    color: 0xf2f6ff, metalness: 0.62, roughness: 0.22, clearcoat: 0.85, clearcoatRoughness: 0.16,
    emissive: 0x1a2740, emissiveIntensity: 0.25,
  });
  const accentMat = new THREE.MeshStandardMaterial({
    color: 0xe8354f, metalness: 0.35, roughness: 0.36, emissive: 0x4a0010, emissiveIntensity: 0.65,
  });
  const goldMat = new THREE.MeshStandardMaterial({
    color: 0xd4af37, metalness: 0.95, roughness: 0.28, emissive: 0x3a2a00, emissiveIntensity: 0.35,
  });
  const darkMat = new THREE.MeshStandardMaterial({ color: 0x2a2f3d, metalness: 0.9, roughness: 0.3 });
  const glassMat = new THREE.MeshPhysicalMaterial({
    color: 0x0d2038, metalness: 0.2, roughness: 0.05, clearcoat: 1, clearcoatRoughness: 0.04,
    emissive: 0x6fd8ff, emissiveIntensity: 1.35,
  });

  // Sleeker, longer fuselage with a gentle taper to the tail.
  const BODY = 3.1;
  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.5, 0.62, BODY, 32), bodyMat);
  body.position.y = 0.1;
  hull.add(body);

  // Long tapered nose cone for a sharper, faster silhouette.
  const nose = new THREE.Mesh(new THREE.ConeGeometry(0.5, 1.5, 32), accentMat);
  nose.position.y = body.position.y + BODY / 2 + 1.5 / 2 - 0.03;
  hull.add(nose);
  // A small chromed tip cap on the very point of the nose.
  const tip = new THREE.Mesh(new THREE.SphereGeometry(0.09, 16, 16), goldMat);
  tip.position.y = nose.position.y + 1.5 / 2 - 0.02;
  hull.add(tip);

  const collar = new THREE.Mesh(new THREE.TorusGeometry(0.53, 0.055, 12, 28), goldMat);
  collar.rotation.x = Math.PI / 2;
  collar.position.y = body.position.y + BODY / 2 - 0.04;
  hull.add(collar);

  // Two slim gold trim bands around the fuselage.
  for (const yy of [0.55, -0.55]) {
    const band = new THREE.Mesh(new THREE.CylinderGeometry(0.585, 0.6, 0.09, 32), goldMat);
    band.position.y = yy;
    hull.add(band);
  }

  const nozzle = new THREE.Mesh(new THREE.CylinderGeometry(0.62, 0.42, 0.55, 28), darkMat);
  nozzle.position.y = body.position.y - BODY / 2 - 0.55 / 2 + 0.03;
  hull.add(nozzle);
  const nozzleThroat = new THREE.Mesh(
    new THREE.CylinderGeometry(0.32, 0.32, 0.08, 20),
    new THREE.MeshStandardMaterial({ color: 0x180a05, emissive: 0xff7a28, emissiveIntensity: 0.8, roughness: 0.6 })
  );
  nozzleThroat.position.y = nozzle.position.y - 0.26;
  hull.add(nozzleThroat);

  const windowGlass = new THREE.Mesh(new THREE.SphereGeometry(0.24, 20, 20), glassMat);
  windowGlass.position.set(0, 0.82, 0.5);
  hull.add(windowGlass);
  const windowRing = new THREE.Mesh(new THREE.TorusGeometry(0.27, 0.04, 10, 24), goldMat);
  windowRing.position.copy(windowGlass.position);
  hull.add(windowRing);

  // Three swept fins, extruded for real thickness so they pick up rim light
  // along their edges instead of reading as flat cutouts.
  const finShape = new THREE.Shape();
  finShape.moveTo(0, 0.42);
  finShape.lineTo(0, -0.62);
  finShape.lineTo(1.02, -1.02);
  finShape.lineTo(0.30, 0.16);
  finShape.closePath();
  const finGeo = new THREE.ExtrudeGeometry(finShape, { depth: 0.08, bevelEnabled: true, bevelThickness: 0.02, bevelSize: 0.015, bevelSegments: 2 });
  finGeo.translate(0, 0, -0.04);
  for (let i = 0; i < 3; i++) {
    const pivot = new THREE.Group();
    pivot.rotation.y = (i / 3) * Math.PI * 2;
    pivot.position.y = -0.95;
    hull.add(pivot);
    const fin = new THREE.Mesh(finGeo, accentMat);
    fin.position.x = 0.55;
    pivot.add(fin);
  }

  rocket.scale.setScalar(1.45);

  // The skywriting rocket flies through the same depth range as the URTO
  // logo/orbs/rings, which would otherwise clip through it. Like the flame
  // and trail text, it always draws on top; renderOrder keeps its own parts
  // (nose over body, etc.) compositing in a sane back-to-front order.
  let order = 0;
  rocket.traverse((obj) => {
    if (obj.isMesh) {
      obj.material.depthTest = false;
      obj.material.depthWrite = false;
      obj.renderOrder = 8 + (order++);
    }
  });
  return rocket;
}

/* Layered engine flame: a hot core + a softer outer glow + a real point
 * light, each flickering independently for a busier, more "alive" burn. */
function buildFlameGroup() {
  const group = new THREE.Group();
  const core = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeRadialTexture('rgba(255,252,235,1)', 'rgba(255,220,140,0)'),
    transparent: true, blending: THREE.AdditiveBlending, depthTest: false, depthWrite: false,
  }));
  core.scale.set(1.6, 1.05, 1);
  group.add(core);

  const outer = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeRadialTexture('rgba(255,196,110,0.95)', 'rgba(255,96,36,0)'),
    transparent: true, blending: THREE.AdditiveBlending, depthTest: false, depthWrite: false,
  }));
  outer.scale.set(3.6, 2.1, 1);
  group.add(outer);

  // A broad, dim halo so the engine casts a soft warm bloom onto the trail.
  const halo = new THREE.Sprite(new THREE.SpriteMaterial({
    map: makeRadialTexture('rgba(255,150,70,0.55)', 'rgba(255,90,40,0)'),
    transparent: true, blending: THREE.AdditiveBlending, depthTest: false, depthWrite: false, opacity: 0.6,
  }));
  halo.scale.set(6.0, 4.2, 1);
  group.add(halo);

  const light = new THREE.PointLight(0xffa64d, 70, 20, 2);
  group.add(light);

  group.renderOrder = 7;
  return { group, core, outer, halo, light };
}

/* The skywriting itself: a GPU point-cloud sampled from the message, revealed
 * column-by-column as the rocket passes so the letters form out of drifting
 * smoke. One draw call, animated entirely in the shader from three uniforms
 * (write progress, time, global fade). */
function buildSkywriting(worldW) {
  const s = sampleTextPoints(FLYBY_TEXT, worldW);
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.BufferAttribute(s.positions, 3));
  geo.setAttribute('aReveal', new THREE.BufferAttribute(s.reveals, 1));
  geo.setAttribute('aSeed', new THREE.BufferAttribute(s.seeds, 1));
  geo.computeBoundingSphere();

  const mat = new THREE.ShaderMaterial({
    uniforms: {
      uProgress: { value: 0 },
      uTime: { value: 0 },
      uFade: { value: 1 },
      uSize: { value: 2.5 },
      uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) },
      uColor: { value: new THREE.Color(0x7286b6) },
      uTex: { value: makeSmokePointTexture() },
    },
    vertexShader: `
      attribute float aReveal;
      attribute float aSeed;
      uniform float uProgress, uTime, uSize, uPixelRatio;
      varying float vAlpha;
      void main() {
        float age = uProgress - aReveal;          // time since this column was written
        float appear = smoothstep(0.0, 0.025, age);
        float grow = smoothstep(0.0, 0.10, age);  // puff outward just after written
        // Let the oldest smoke thin out a little so the trail doesn't build to
        // an overexposed slab on the left — keeps the whole message legible.
        vAlpha = appear * (1.0 - 0.32 * smoothstep(0.25, 1.1, age));
        float t = max(age, 0.0);
        float sway  = sin(uTime * 0.9 + aSeed * 6.2831);
        float sway2 = cos(uTime * 0.7 + aSeed * 12.566);
        vec3 pos = position;
        pos.x += (-0.55 * t) + sway * 0.20 * t;   // drift back along the trail
        pos.y += 0.40 * t + sway2 * 0.18 * t;     // gentle rise + billow
        pos.z += sway * 0.28 * t;
        vec4 mv = modelViewMatrix * vec4(pos, 1.0);
        float size = uSize * (0.35 + 0.65 * grow) * (1.0 + t * 0.18);
        gl_PointSize = size * uPixelRatio * (300.0 / -mv.z);
        gl_Position = projectionMatrix * mv;
      }
    `,
    fragmentShader: `
      uniform sampler2D uTex;
      uniform vec3 uColor;
      uniform float uFade;
      varying float vAlpha;
      void main() {
        float a = texture2D(uTex, gl_PointCoord).a * vAlpha * uFade;
        if (a < 0.01) discard;
        gl_FragColor = vec4(uColor, a);
      }
    `,
    transparent: true,
    blending: THREE.AdditiveBlending,
    depthTest: false,
    depthWrite: false,
  });

  const points = new THREE.Points(geo, mat);
  points.frustumCulled = false;
  points.renderOrder = 5;
  points.visible = false;
  return { points, mat };
}

function buildFlyby() {
  const worldW = 52;
  const sky = buildSkywriting(worldW);
  scene.add(sky.points);

  const rocket = buildRocketGroup();
  rocket.visible = false;
  scene.add(rocket);

  const flame = buildFlameGroup();
  flame.group.visible = false;
  scene.add(flame.group);

  // Dense exhaust puffs right at the engine bell — a thick plume close to the
  // rocket that the skywriting smoke then trails away from.
  const puffTex = makePuffTexture();
  const puffs = [];
  for (let i = 0; i < 64; i++) {
    const m = new THREE.SpriteMaterial({ map: puffTex, transparent: true, opacity: 0, depthTest: false, depthWrite: false });
    const sp = new THREE.Sprite(m);
    sp.visible = false;
    sp.renderOrder = 4;
    scene.add(sp);
    puffs.push({ sp, life: 0, maxLife: 1, vx: 0, vy: 0, vz: 0, spin: 0, baseScale: 1, sx: 1, sy: 1 });
  }

  // Spark pool: tiny bright motes kicked off the exhaust for extra sparkle.
  const sparkTex = makeSparkTexture();
  const sparks = [];
  for (let i = 0; i < 24; i++) {
    const m = new THREE.SpriteMaterial({ map: sparkTex, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthTest: false, depthWrite: false });
    const sp = new THREE.Sprite(m);
    sp.visible = false;
    sp.renderOrder = 6;
    scene.add(sp);
    sparks.push({ sp, life: 0, maxLife: 1, vx: 0, vy: 0, vz: 0, baseScale: 1 });
  }

  flyby = {
    sky, rocket, flame, worldW,
    puffs, puffCursor: 0, puffTimer: 0,
    sparks, sparkCursor: 0,
    state: 'idle',
    nextAt: 6,             // first flyby a few seconds in
    t: 0,
    lead: 2.6,             // rocket nose rides just ahead of the fresh smoke
    y: 9, z: -4,
    duration: 8,
  };
}

function spawnPuff(f, x, y, z) {
  const p = f.puffs[f.puffCursor];
  f.puffCursor = (f.puffCursor + 1) % f.puffs.length;
  p.life = 0;
  p.maxLife = 1.4 + Math.random() * 1.1;
  p.vx = -2.0 - Math.random() * 1.4;              // drift back along the trail
  p.vy = (Math.random() - 0.5) * 0.7;
  p.vz = (Math.random() - 0.5) * 0.9;             // spread in depth for volume
  p.spin = (Math.random() - 0.5) * 0.9;
  p.baseScale = 1.2 + Math.random() * 1.3;
  p.sx = 0.85 + Math.random() * 0.3;
  p.sy = 0.85 + Math.random() * 0.3;
  p.sp.position.set(x + (Math.random() - 0.5) * 0.6, y + (Math.random() - 0.5) * 0.6, z + (Math.random() - 0.5) * 1.4);
  p.sp.material.rotation = Math.random() * Math.PI * 2;
  p.sp.scale.set(p.baseScale * p.sx, p.baseScale * p.sy, 1);
  p.sp.material.opacity = 0.3;
  p.sp.visible = true;
}

function updatePuffs(f, dt) {
  for (const p of f.puffs) {
    if (!p.sp.visible) continue;
    p.life += dt;
    const k = p.life / p.maxLife;
    if (k >= 1) { p.sp.visible = false; continue; }
    p.sp.position.x += p.vx * dt;
    p.sp.position.y += p.vy * dt;
    p.sp.position.z += p.vz * dt;
    p.sp.material.rotation += p.spin * dt;
    const bloom = p.baseScale * (1 + k * 1.9);
    p.sp.scale.set(bloom * p.sx, bloom * p.sy, 1);      // billow outward, non-uniformly
    p.sp.material.opacity = 0.3 * (1 - k);
  }
}

function spawnSpark(f, x, y, z) {
  const p = f.sparks[f.sparkCursor];
  f.sparkCursor = (f.sparkCursor + 1) % f.sparks.length;
  p.life = 0;
  p.maxLife = 0.35 + Math.random() * 0.35;
  p.vx = -3.5 - Math.random() * 2.5;
  p.vy = (Math.random() - 0.5) * 2.6;
  p.vz = (Math.random() - 0.5) * 1.6;
  p.baseScale = 0.25 + Math.random() * 0.3;
  p.sp.position.set(x, y, z);
  p.sp.scale.setScalar(p.baseScale);
  p.sp.material.opacity = 1;
  p.sp.visible = true;
}

function updateSparks(f, dt) {
  for (const p of f.sparks) {
    if (!p.sp.visible) continue;
    p.life += dt;
    const k = p.life / p.maxLife;
    if (k >= 1) { p.sp.visible = false; continue; }
    p.vy -= dt * 1.4; // slight gravity droop
    p.sp.position.x += p.vx * dt;
    p.sp.position.y += p.vy * dt;
    p.sp.position.z += p.vz * dt;
    p.sp.scale.setScalar(p.baseScale * (1 - k * 0.6));
    p.sp.material.opacity = 1 - k;
  }
}

function updateFlyby(dt, now) {
  if (!flyby) return;
  const f = flyby;

  updatePuffs(f, dt);
  updateSparks(f, dt);

  // Keep the smoke's own sway/billow animating in every active phase.
  if (f.state !== 'idle') f.sky.mat.uniforms.uTime.value = now;

  if (f.state === 'idle') {
    if (now >= f.nextAt) {
      f.state = 'writing';
      f.t = 0;
      f.y = 6 + Math.random() * 5;           // low enough that the rocket stays on-screen
      f.z = -5 + Math.random() * 6;
      f.duration = 7.5 + Math.random() * 2;
      f.puffTimer = 0;
      f.sky.points.position.set(0, f.y, f.z);
      f.sky.mat.uniforms.uProgress.value = 0;
      f.sky.mat.uniforms.uFade.value = 1;
      f.sky.mat.uniforms.uTime.value = now;
      f.sky.points.visible = true;
      f.rocket.visible = true;
      f.flame.group.visible = true;
    }
    return;
  }

  const left = -f.worldW / 2, right = f.worldW / 2;

  if (f.state === 'writing') {
    f.t += dt / f.duration;
    const p = Math.min(f.t, 1);
    f.sky.mat.uniforms.uProgress.value = p;

    // Straight, level flight — no vertical bob. The rocket's nose rides just
    // ahead of the freshly written column so the smoke pours from its engine.
    const writeX = left + p * f.worldW;
    const rx = writeX + f.lead;
    const ry = f.y;
    f.rocket.position.set(rx, ry, f.z + 0.6);
    // A slow barrel-roll around the flight axis (+X) gives life without ever
    // moving the rocket off its level line.
    f.rocket.rotation.set(Math.sin(now * 1.6) * 0.14, 0, 0);

    const nozzleX = rx - 2.6, nozzleY = ry, nozzleZ = f.z + 0.6;
    f.flame.group.position.set(nozzleX, nozzleY, nozzleZ);
    const flick = 0.85 + Math.random() * 0.3;
    f.flame.core.scale.set((1.35 + Math.random() * 0.35) * flick, 1.0 + Math.random() * 0.3, 1);
    f.flame.core.material.opacity = 0.9 + Math.random() * 0.1;
    f.flame.outer.scale.set((3.2 + Math.random() * 1.0) * flick, 1.9 + Math.random() * 0.5, 1);
    f.flame.outer.material.opacity = 0.6 + Math.random() * 0.35;
    f.flame.halo.material.opacity = 0.45 + Math.random() * 0.2;
    f.flame.light.intensity = 60 + Math.random() * 35;

    // Thick exhaust plume + occasional sparks straight out of the engine bell.
    f.puffTimer += dt;
    while (f.puffTimer > 0.03) {
      f.puffTimer -= 0.03;
      spawnPuff(f, nozzleX - 0.3, ry, f.z + 0.4);
      if (Math.random() < 0.6) spawnSpark(f, nozzleX - 0.3, nozzleY, nozzleZ);
    }

    if (p >= 1) {
      f.state = 'holding';
      f.t = 0;
      f.rocket.visible = false;
      f.flame.group.visible = false;
    }
    return;
  }

  if (f.state === 'holding') {
    f.t += dt;
    if (f.t > 2.8) { f.state = 'fading'; f.t = 0; }
    return;
  }

  if (f.state === 'fading') {
    f.t += dt / 3.4;
    f.sky.mat.uniforms.uFade.value = 1 - Math.min(f.t, 1);
    f.sky.points.position.y = f.y + f.t * 1.8;       // whole trail drifts up as it dissipates
    if (f.t >= 1) {
      f.sky.points.visible = false;
      f.sky.points.position.y = f.y;
      f.state = 'idle';
      f.nextAt = now + 26 + Math.random() * 24;      // next flyby in ~26–50s
    }
    return;
  }
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
  buildFlyby();

  composer = new EffectComposer(renderer);
  composer.addPass(new RenderPass(scene, camera));
  const bloom = new UnrealBloomPass(new THREE.Vector2(window.innerWidth, window.innerHeight), 0.5, 0.45, 0.6);
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
    orb.glow.scale.setScalar(8.5 * (1 + orb.hoverT * 0.45));
    orb.core.material.emissiveIntensity = 1.3 + orb.hoverT * 1.4;
    orb.light.intensity = 90 + orb.hoverT * 120;
  }

  updateFlyby(dt, t);
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
