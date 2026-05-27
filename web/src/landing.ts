// Landing intro overlay — shown on every page load.
// Renders a full-viewport spinning Earth with the "So What?" title, a tagline,
// and three feature bullets that fade in one by one. Resolves a Promise when
// the user clicks "Launch" (or after 12 s auto-advance), letting main.ts flip
// the view to Globe mode.
//
// Uses the same Three.js + world-atlas TopoJSON stack as render3d.ts but is
// fully self-contained (no graphology, no 3d-force-graph, no data nodes).

/* eslint-disable @typescript-eslint/no-explicit-any */

import * as THREE from "three";
import landTopo from "world-atlas/land-110m.json";
import { feature as topoFeature } from "topojson-client";

// ── Globe geometry ────────────────────────────────────────────────────────────

const LAND_R = 200;

function ll2xyz(lat: number, lon: number, r = LAND_R): THREE.Vector3 {
  const la = (lat * Math.PI) / 180;
  const lo = (lon * Math.PI) / 180;
  return new THREE.Vector3(
    r * Math.cos(la) * Math.sin(lo),
    r * Math.sin(la),
    r * Math.cos(la) * Math.cos(lo),
  );
}

function makeLandGeo(): THREE.BufferGeometry {
  const decoded: any = topoFeature(landTopo as any, (landTopo as any).objects.land);
  const pos: number[] = [];
  const r = LAND_R + 0.5;

  const ring = (coords: number[][]) => {
    let prev: THREE.Vector3 | null = null;
    for (const [lo, la] of coords) {
      const p = ll2xyz(la, lo, r);
      if (prev) pos.push(prev.x, prev.y, prev.z, p.x, p.y, p.z);
      prev = p;
    }
  };

  const walk = (g: any) => {
    if (!g) return;
    if      (g.type === "MultiPolygon")    g.coordinates.forEach((poly: number[][][]) => poly.forEach(ring));
    else if (g.type === "Polygon")         g.coordinates.forEach(ring);
    else if (g.type === "GeometryCollection") g.geometries?.forEach(walk);
  };

  if (decoded?.type === "FeatureCollection") decoded.features?.forEach((f: any) => walk(f.geometry));
  else walk(decoded);

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  return geo;
}

function makeGraticuleGeo(): THREE.BufferGeometry {
  const pos: number[] = [];
  const r = LAND_R + 0.2;
  const N = 72;

  // Latitude parallels at ±30° and ±60°
  for (const lat of [-60, -30, 0, 30, 60]) {
    let prev: THREE.Vector3 | null = null;
    for (let i = 0; i <= N; i++) {
      const p = ll2xyz(lat, (i / N) * 360 - 180, r);
      if (prev) pos.push(prev.x, prev.y, prev.z, p.x, p.y, p.z);
      prev = p;
    }
  }
  // Meridians every 30°
  for (let lon = -180; lon < 180; lon += 30) {
    let prev: THREE.Vector3 | null = null;
    for (let i = 0; i <= N; i++) {
      const p = ll2xyz((i / N) * 180 - 90, lon, r);
      if (prev) pos.push(prev.x, prev.y, prev.z, p.x, p.y, p.z);
      prev = p;
    }
  }

  const geo = new THREE.BufferGeometry();
  geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
  return geo;
}

// ── HTML template ─────────────────────────────────────────────────────────────

function buildHTML(): string {
  // Each character of the title gets its own <span> with a staggered CSS
  // animation-delay so the word materialises left-to-right.
  const title = "So What?";
  const chars = [...title].map((ch, i) => {
    const delay = (0.9 + i * 0.07).toFixed(2);
    // The question mark gets a cyan accent class.
    const inner = ch === " "
      ? "&nbsp;"
      : ch === "?" ? `<span class="landing-q">${ch}</span>` : ch;
    return `<span class="lc" style="animation-delay:${delay}s">${inner}</span>`;
  }).join("");

  return `
<canvas id="landing-canvas"></canvas>
<div class="landing-content">
  <div class="landing-eyebrow">EconGraph</div>
  <h1 class="landing-title" aria-label="${title}">${chars}</h1>
  <p class="landing-tagline">Every headline, traced to every company it touches.</p>
  <ul class="landing-features">
    <li class="lf" style="animation-delay:3.1s">
      <span class="lf-dot">◉</span>
      <span>2 400+ entities mapped across 11 GICS sectors</span>
    </li>
    <li class="lf" style="animation-delay:3.45s">
      <span class="lf-dot">⚡</span>
      <span>Real-time supply chain &amp; impact propagation</span>
    </li>
    <li class="lf" style="animation-delay:3.8s">
      <span class="lf-dot">🌐</span>
      <span>Competition, regulation &amp; trade routes — global</span>
    </li>
  </ul>
  <button id="landing-enter" class="landing-enter" type="button">
    Launch <span class="landing-arrow">→</span>
  </button>
  <button id="landing-skip" class="landing-skip" type="button">skip intro</button>
</div>`;
}

// ── Public entry point ────────────────────────────────────────────────────────

/** Show the landing overlay. Returns a Promise that resolves when the user
 *  dismisses it (click Launch, press any key after 2 s, or 12 s auto-advance).
 *  Call `setView("globe")` in the `.then()` handler. */
export function runLanding(): Promise<void> {
  return new Promise((resolve) => {
    const overlay = document.createElement("div");
    overlay.id = "landing-overlay";
    overlay.innerHTML = buildHTML();
    document.body.appendChild(overlay);

    // ── Three.js globe scene ─────────────────────────────────────────────────
    const canvas = overlay.querySelector<HTMLCanvasElement>("#landing-canvas")!;

    const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    renderer.setClearColor(0x000000, 0);
    renderer.setSize(window.innerWidth, window.innerHeight);

    const scene = new THREE.Scene();
    const cam = new THREE.PerspectiveCamera(
      45,
      window.innerWidth / window.innerHeight,
      1,
      2000,
    );
    cam.position.z = 590;

    // Ocean sphere — very deep blue-black
    const oceanGeo = new THREE.SphereGeometry(LAND_R, 72, 72);
    const oceanMat = new THREE.MeshPhongMaterial({
      color:    0x070d18,
      emissive: 0x030810,
      shininess: 12,
    });
    const ocean = new THREE.Mesh(oceanGeo, oceanMat);

    // Land outlines from TopoJSON
    const landGeo = makeLandGeo();
    const landMat = new THREE.LineBasicMaterial({ color: 0x1e3a56 });
    const land = new THREE.LineSegments(landGeo, landMat);

    // Graticule — very subtle grid lines
    const gratGeo = makeGraticuleGeo();
    const gratMat = new THREE.LineBasicMaterial({
      color: 0x0c1c2c,
      transparent: true,
      opacity: 0.65,
    });
    const grat = new THREE.LineSegments(gratGeo, gratMat);

    // Atmosphere — backside-rendered glow ring
    const atmosGeo = new THREE.SphereGeometry(LAND_R * 1.055, 32, 32);
    const atmosMat = new THREE.MeshPhongMaterial({
      color:       0x1a4a8a,
      transparent: true,
      opacity:     0.06,
      side:        THREE.BackSide,
    });
    const atmos = new THREE.Mesh(atmosGeo, atmosMat);

    // All globe parts share a pivot for rotation
    const pivot = new THREE.Group();
    pivot.add(ocean, land, grat, atmos);
    // Start with the Atlantic facing the camera (Americas left, Europe right)
    pivot.rotation.y = -Math.PI * 0.28;
    scene.add(pivot);

    // Lighting — cool blue ambient + warm directional from upper-right
    scene.add(new THREE.AmbientLight(0x25385a, 0.55));
    const dir = new THREE.DirectionalLight(0x4a7fbf, 0.85);
    dir.position.set(2, 1, 2.5);
    scene.add(dir);

    // Render loop — very slow westward rotation
    let rafId = 0;
    const t0 = performance.now();
    const tick = () => {
      rafId = requestAnimationFrame(tick);
      pivot.rotation.y = -Math.PI * 0.28 + ((performance.now() - t0) / 1000) * 0.055;
      renderer.render(scene, cam);
    };
    tick();

    // Keep canvas filling the viewport on resize
    const onResize = () => {
      cam.aspect = window.innerWidth / window.innerHeight;
      cam.updateProjectionMatrix();
      renderer.setSize(window.innerWidth, window.innerHeight);
    };
    window.addEventListener("resize", onResize);

    // ── Dismiss ──────────────────────────────────────────────────────────────
    let gone = false;
    const dismiss = () => {
      if (gone) return;
      gone = true;
      overlay.classList.add("landing-exit");
      cancelAnimationFrame(rafId);
      window.removeEventListener("resize", onResize);
      // Fade-out lasts 0.9 s (matches CSS transition), then clean up.
      setTimeout(() => {
        renderer.dispose();
        [oceanGeo, landGeo, gratGeo, atmosGeo].forEach((g) => g.dispose());
        [oceanMat, landMat, gratMat, atmosMat].forEach((m) => m.dispose());
        overlay.remove();
        resolve();
      }, 900);
    };

    // Only the two explicit buttons dismiss the overlay — no keydown listener
    // (HMR focus events and tab switches were triggering it accidentally).
    overlay.querySelector("#landing-enter")?.addEventListener("click", dismiss);
    overlay.querySelector("#landing-skip")?.addEventListener("click", dismiss);
  });
}
