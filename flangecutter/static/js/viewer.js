// 3D view: the model, one plane per cut with a move/rotate gizmo, the preview overlays, and the
// printed parts after Generate. Z is up, as in the STL file and in PrusaSlicer. Lengths are mm.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { TransformControls } from 'three/addons/controls/TransformControls.js';
import { STLLoader } from 'three/addons/loaders/STLLoader.js';

export const CUT_COLORS = ['#6d4cff', '#0f8b8d', '#d6336c', '#5c940d', '#1c7ed6', '#8d5524'];
export const PART_COLORS = ['#2f74d0', '#f08c00', '#2b9348', '#9c36b5', '#d9480f', '#1098ad',
  '#c2255c', '#5f3dc4', '#74b816', '#495057'];
const MAX_CUTS = 6;
const MODEL_GRAY = '#c5cbd3', FLANGE = '#f2a516', SECTION = '#2b2f36', BOLT = '#3b4652', BLOCKED = '#e03131';
const vec = (a) => new THREE.Vector3(a[0], a[1], a[2]);
const tidy = (v) => Math.round(v * 1e6) / 1e6 + 0;        // + 0 turns -0 into 0

/** In-plane x axis for a normal, chosen so the 2D cut view shows the model upright (+Z up) when it can. */
export function niceFrame(normal) {
  const n = vec(normal).normalize();
  let x;
  if (Math.abs(n.z) > 0.9) {                       // a horizontal cut: x along +X
    x = new THREE.Vector3(1, 0, 0).addScaledVector(n, -n.x);
  } else {                                         // "up" in the view is +Z, so x = up × normal
    x = new THREE.Vector3(0, 0, 1).addScaledVector(n, -n.z).cross(n);
  }
  x.normalize();
  return { normal: n.toArray().map(tidy), x_axis: x.toArray().map(tidy) };
}

/** Rotation of a plane object: its local +X is the in-plane x axis, its local +Z the normal. */
function planeQuaternion(plane) {
  const n = vec(plane.normal).normalize();
  const x = vec(plane.x_axis);
  x.addScaledVector(n, -x.dot(n));
  if (x.lengthSq() < 1e-10) return planeQuaternion(niceFrame(plane.normal));
  x.normalize();
  const y = new THREE.Vector3().crossVectors(n, x);
  return new THREE.Quaternion().setFromRotationMatrix(new THREE.Matrix4().makeBasis(x, y, n));
}

/** A round badge with a number or letter. It faces the camera and keeps its size on screen. */
function badge(text, color) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = 128;
  const g = canvas.getContext('2d');
  g.fillStyle = color;
  g.strokeStyle = '#ffffff';
  g.lineWidth = 8;
  g.beginPath();
  g.arc(64, 64, 56, 0, 2 * Math.PI);
  g.fill();
  g.stroke();
  g.fillStyle = '#ffffff';
  g.font = 'bold 68px "Segoe UI", Arial, sans-serif';
  g.textAlign = 'center';
  g.textBaseline = 'middle';
  g.fillText(text, 64, 69);
  const map = new THREE.CanvasTexture(canvas);
  map.colorSpace = THREE.SRGBColorSpace;
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map, depthTest: false, sizeAttenuation: false }));
  sprite.scale.setScalar(0.028);
  sprite.renderOrder = 10;
  return sprite;
}

function dispose(root) {
  root.traverse((o) => {
    o.geometry?.dispose();
    for (const m of [o.material].flat()) {
      m?.map?.dispose();
      m?.dispose();
    }
  });
}

/** Model material. The tint colors every point by the side of every cut it lies on, so the user sees
 *  the parts before Generate, in the colors and letter order the parts get after Generate. */
function modelMaterial(tint) {
  const m = new THREE.MeshStandardMaterial({ color: MODEL_GRAY, roughness: 0.8, side: THREE.DoubleSide,
    polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 });   // keeps lines on the surface visible
  m.onBeforeCompile = (shader) => {
    Object.assign(shader.uniforms, tint);
    shader.vertexShader = shader.vertexShader
      .replace('#include <common>', '#include <common>\nvarying vec3 vWorld;')
      .replace('#include <begin_vertex>', '#include <begin_vertex>\nvWorld = (modelMatrix * vec4(transformed, 1.0)).xyz;');
    shader.fragmentShader = shader.fragmentShader
      .replace('#include <common>', `#include <common>
varying vec3 vWorld;
uniform int cutCount;
uniform vec3 cutOrigin[${MAX_CUTS}];
uniform vec3 cutNormal[${MAX_CUTS}];
uniform vec3 cellColor[${2 ** MAX_CUTS}];`)
      .replace('#include <color_fragment>', `#include <color_fragment>
if (cutCount > 0) {
  int key = 0;    // one bit per cut, cut 1 first: 0 = arrow side, 1 = other side
  for (int i = 0; i < ${MAX_CUTS}; i++) {
    if (i < cutCount) key = key * 2 + (dot(vWorld - cutOrigin[i], cutNormal[i]) < 0.0 ? 1 : 0);
  }
  diffuseColor.rgb = cellColor[key];
}`);
  };
  return m;
}

/** 2D points of a ring from the server (shapely repeats the first point at the end). */
function ring(points) {
  const v = points.map(([x, y]) => new THREE.Vector2(x, y));
  if (v.length > 1 && v[0].equals(v[v.length - 1])) v.pop();
  return v;
}

/** One cut's preview, drawn in the frame the server used: flange slab, wall outline, bolts, crossings. */
function buildOverlay(cut, size) {
  const f = cut.frame, d = cut.derived, g = new THREE.Group();
  g.matrixAutoUpdate = false;
  g.matrix.makeBasis(vec(f.x_axis), vec(f.y_axis), vec(f.normal)).setPosition(vec(f.origin));
  // the flange as it will be printed: the 2D outline from -T to +T around the plane
  const shapes = cut.flange.map((p) => {
    const s = new THREE.Shape(ring(p.exterior));
    s.holes = p.holes.map((h) => new THREE.Path(ring(h)));
    return s;
  });
  const slab = new THREE.ExtrudeGeometry(shapes, { depth: 2 * d.thickness, bevelEnabled: false });
  slab.translate(0, 0, -d.thickness);
  g.add(new THREE.Mesh(slab, new THREE.MeshStandardMaterial({ color: FLANGE, roughness: 0.7 })));
  // where the plane meets the wall
  const line = new THREE.LineBasicMaterial({ color: SECTION });
  for (const p of cut.section) {
    for (const r of [p.exterior, ...p.holes]) {
      g.add(new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(ring(r)), line));
    }
  }
  // bolts, red where the wall blocks the tool
  const r = d.hole_diameter / 2;
  const rod = new THREE.CylinderGeometry(r, r, 2 * d.thickness + 6, 16).rotateX(Math.PI / 2);
  const ok = new THREE.MeshStandardMaterial({ color: BOLT }), bad = new THREE.MeshStandardMaterial({ color: BLOCKED });
  for (const b of cut.bolts) {
    const m = new THREE.Mesh(rod, b.blocked ? bad : ok);
    m.position.set(b.xy[0], b.xy[1], 0);
    g.add(m);
  }
  // other cuts that cross this one
  const dash = new THREE.LineDashedMaterial({ color: SECTION, dashSize: size / 60, gapSize: size / 90 });
  for (const c of cut.crossings) {
    const l = new THREE.Line(new THREE.BufferGeometry().setFromPoints(c.line.map(([x, y]) => new THREE.Vector2(x, y))), dash);
    l.computeLineDistances();
    g.add(l);
  }
  return g;
}

export class Viewer {
  /** events: select(id), planeMoved(id, dragging), pickPoint([x, y, z]). */
  constructor(container, events) {
    this.events = events;
    this.container = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio);
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene();
    this.scene.background = new THREE.Color('#e9edf2');
    this.camera = new THREE.PerspectiveCamera(35, 1, 1, 5000);
    this.camera.up.set(0, 0, 1);                       // Z up; set before OrbitControls reads it
    this.camera.position.set(-300, -500, 300);
    const light = new THREE.DirectionalLight('#ffffff', 2.2);   // a headlight that turns with the camera
    light.position.set(-0.5, 0.6, 0);
    light.target.position.set(0, 0, -1);
    this.camera.add(light, light.target);
    this.scene.add(this.camera, new THREE.HemisphereLight('#ffffff', '#8b95a3', 1.2));

    this.orbit = new OrbitControls(this.camera, this.renderer.domElement);
    this.orbit.addEventListener('change', () => this.render());
    this.gizmo = new TransformControls(this.camera, this.renderer.domElement);
    this.gizmo.setSpace('local');                      // the arrow and the rings follow the plane
    this.gizmo.addEventListener('change', () => this.render());
    // A press on the gizmo that moves the mouse less than 4 px is a click: the cut stays where it is, so
    // parts made before are kept. These listeners see the mouse before the gizmo does (capture phase).
    container.addEventListener('pointerdown', (e) => { this.press = { x: e.clientX, y: e.clientY, far: false }; }, true);
    container.addEventListener('pointermove', (e) => {
      const p = this.press;
      if (p && !p.far) p.far = Math.hypot(e.clientX - p.x, e.clientY - p.y) >= 4;
    }, true);
    this.gizmo.addEventListener('dragging-changed', (e) => {
      this.orbit.enabled = !e.value;                   // the view must not turn while a cut is dragged
      const o = this.gizmo.object;
      if (e.value) this.drag = { moved: false, position: o.position.clone(), quaternion: o.quaternion.clone() };
      else if (this.drag?.moved && this.selected) this.events.planeMoved(this.selected, false);
    });
    this.gizmo.addEventListener('objectChange', () => {
      const o = this.gizmo.object, d = this.drag;
      if (!d) return;
      if (!this.press?.far) {                          // not a drag (yet): back to where it started
        o.position.copy(d.position);
        o.quaternion.copy(d.quaternion);
        return;
      }
      d.moved = true;
      this.updateLooks();
      this.events.planeMoved(this.selected, true);
    });
    this.scene.add(this.gizmo.getHelper());
    this.setGizmoMode('translate');

    this.tint = {
      cutCount: { value: 0 },
      cutOrigin: { value: Array.from({ length: MAX_CUTS }, () => new THREE.Vector3()) },
      cutNormal: { value: Array.from({ length: MAX_CUTS }, () => new THREE.Vector3()) },
      cellColor: { value: Array.from({ length: 2 ** MAX_CUTS }, () => new THREE.Color(MODEL_GRAY)) },
    };
    this.cutGroup = new THREE.Group();
    this.overlayGroup = new THREE.Group();
    this.partGroup = new THREE.Group();
    this.scene.add(this.cutGroup, this.overlayGroup, this.partGroup);
    this.cuts = new Map();          // cut id -> {obj, rect, fill, edge, arrow, label, number}
    this.order = [];                // cut ids in cut order (cut 1 first)
    this.overlays = new Map();      // cut id -> overlay group
    this.model = null;
    this.selected = null;
    this.showingParts = false;
    this.raycaster = new THREE.Raycaster();
    this.listenForClicks();
    new ResizeObserver(() => this.resize()).observe(container);
    this.resize();
  }

  render() {                        // draw on the next frame, once, only when something changed
    if (this.frame) return;
    this.frame = requestAnimationFrame(() => {
      this.frame = 0;
      this.placeMarks();
      this.renderer.render(this.scene, this.camera);
    });
  }

  /** Put each cut's number and arrow where they stay in sight: on the corner of its plane nearest to the
   *  camera among the corners on screen; when no corner is on screen, on the point of the plane nearest
   *  to the middle of the view. */
  placeMarks() {
    this.scene.updateMatrixWorld();
    const p = new THREE.Vector3(), inset = 0.05 * this.size + 2, clamp = THREE.MathUtils.clamp;
    for (const c of this.cuts.values()) {
      const { x: w, y: h } = c.rect.scale, { x: cx, y: cy } = c.rect.position;
      let best = Infinity, at = null;
      for (const [ax, ay] of [[-1, -1], [1, -1], [1, 1], [-1, 1]]) {
        p.set(cx + (ax * w) / 2, cy + (ay * h) / 2, 0).applyMatrix4(c.obj.matrixWorld);
        const d = p.distanceToSquared(this.camera.position);
        p.project(this.camera);                    // on screen: -1..1, with a margin for the badge
        if (Math.abs(p.x) < 0.9 && Math.abs(p.y) < 0.85 && p.z < 1 && d < best) [best, at] = [d, [ax, ay]];
      }
      if (at) {
        c.label.position.set(cx + (at[0] * w) / 2, cy + (at[1] * h) / 2, 0);
        c.arrow.position.set(cx + at[0] * (w / 2 - inset), cy + at[1] * (h / 2 - inset), 0);
      } else {
        const q = c.obj.worldToLocal(p.copy(this.orbit.target));
        q.set(clamp(q.x, cx - w / 2 + inset, cx + w / 2 - inset), clamp(q.y, cy - h / 2 + inset, cy + h / 2 - inset), 0);
        c.label.position.copy(q);
        c.arrow.position.copy(q);
      }
    }
  }

  resize() {
    const w = this.container.clientWidth, h = this.container.clientHeight;
    if (!w || !h) return;
    this.renderer.setSize(w, h, false);
    this.camera.aspect = w / h;
    this.camera.updateProjectionMatrix();
    this.render();
  }

  /** Show a new model (binary STL) and forget everything about the old one. */
  setModel(buffer, bbox) {
    this.setCuts([], null);
    this.clearParts();
    if (this.model) {
      this.scene.remove(this.model);
      dispose(this.model);
    }
    const geometry = new STLLoader().parse(buffer);
    this.model = new THREE.Mesh(geometry, modelMaterial(this.tint));
    this.scene.add(this.model);
    this.bounds = new THREE.Box3(vec(bbox.min), vec(bbox.max));
    this.center = vec(bbox.center);
    this.size = vec(bbox.size).length();
    // up to 60,000 model points: they tell which cells of the cuts hold material (see updateTint)
    const pos = geometry.attributes.position.array, step = 3 * Math.max(1, Math.ceil(pos.length / 3 / 60000));
    this.samples = [];
    for (let i = 0; i < pos.length; i += step) this.samples.push(pos[i], pos[i + 1], pos[i + 2]);
    this.showParts(false);
    this.fitView();
  }

  /** Look at what is shown (the model, or the parts as far apart as they are) from the front left, above. */
  fitView() {
    if (!this.bounds) return;
    const box = this.showingParts && this.partGroup.children.length ? new THREE.Box3().setFromObject(this.partGroup) : this.bounds;
    const sphere = box.getBoundingSphere(new THREE.Sphere());
    const half = THREE.MathUtils.degToRad(this.camera.fov / 2);
    const dist = (1.1 * sphere.radius) / Math.min(Math.tan(half), Math.tan(half) * this.camera.aspect);
    this.camera.position.copy(sphere.center).addScaledVector(new THREE.Vector3(-0.8, -1, 0.7).normalize(), dist);
    this.camera.near = dist / 100;
    this.camera.far = dist * 20;
    this.camera.updateProjectionMatrix();
    this.orbit.maxDistance = dist * 8;                 // zooming out further would lose the model
    this.orbit.target.copy(sphere.center);
    this.orbit.update();
    this.render();
  }

  /** Does this plane ({origin, normal}) go through the model, that is, has some triangle corners on both sides? */
  crosses(plane) {
    const v = this.model?.geometry.attributes.position.array || [], [nx, ny, nz] = plane.normal;
    const k = nx * plane.origin[0] + ny * plane.origin[1] + nz * plane.origin[2];
    for (let i = 0; i < v.length; i += 9) {        // the loader gives 3 corners (9 numbers) per triangle
      const a = nx * v[i] + ny * v[i + 1] + nz * v[i + 2] - k, b = nx * v[i + 3] + ny * v[i + 4] + nz * v[i + 5] - k;
      const c = nx * v[i + 6] + ny * v[i + 7] + nz * v[i + 8] - k;
      if (Math.min(a, b, c) < 0 && Math.max(a, b, c) > 0) return true;
    }
    return false;
  }

  setSeeThrough(on) {
    if (!this.model) return;
    const m = this.model.material;
    m.transparent = on;
    m.opacity = on ? 0.35 : 1;
    m.depthWrite = !on;
    m.needsUpdate = true;
    this.render();
  }

  // ------------------------------------------------------------ cuts
  /** Show these cuts ([{id, plane}], cut 1 first). Planes are only set for cuts that are new here. */
  setCuts(list, selectedId) {
    for (const [id, c] of this.cuts) {
      if (list.some((x) => x.id === id)) continue;
      if (this.gizmo.object === c.obj) this.gizmo.detach();
      this.cutGroup.remove(c.obj);
      dispose(c.obj);
      this.cuts.delete(id);
      this.setOverlay(id, null);
    }
    list.forEach((item, i) => {
      let c = this.cuts.get(item.id);
      if (!c) {
        c = this.makeCut(item.id);
        this.setPlaneOf(c, item.plane);
      }
      if (c.number !== i + 1) this.numberCut(c, i + 1);
    });
    this.order = list.map((x) => x.id);
    this.select(selectedId);
  }

  makeCut(id) {
    const obj = new THREE.Group(), rect = new THREE.Group();
    const fill = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({
      transparent: true, side: THREE.DoubleSide, depthWrite: false,
      polygonOffset: true, polygonOffsetFactor: 1, polygonOffsetUnits: 1 }));
    const corners = [[-0.5, -0.5], [0.5, -0.5], [0.5, 0.5], [-0.5, 0.5]].map(([x, y]) => new THREE.Vector3(x, y, 0));
    const edge = new THREE.LineLoop(new THREE.BufferGeometry().setFromPoints(corners),
      new THREE.LineBasicMaterial({ transparent: true }));
    const arrow = new THREE.ArrowHelper(new THREE.Vector3(0, 0, 1), new THREE.Vector3(), 1);
    fill.userData.cutId = id;
    rect.add(fill, edge);
    obj.add(rect, arrow);
    this.cutGroup.add(obj);
    const c = { id, obj, rect, fill, edge, arrow, label: null, number: 0 };
    this.cuts.set(id, c);
    return c;
  }

  numberCut(c, number) {
    const color = CUT_COLORS[(number - 1) % CUT_COLORS.length];
    c.number = number;
    c.fill.material.color.set(color);
    c.edge.material.color.set(color);
    c.arrow.setColor(color);
    if (c.label) {
      c.obj.remove(c.label);
      dispose(c.label);
    }
    c.label = badge(String(number), color);
    c.label.userData.cutId = c.id;
    c.obj.add(c.label);
  }

  select(id) {
    this.selected = this.cuts.has(id) ? id : null;
    const c = this.cuts.get(this.selected);
    if (c && !this.showingParts) this.gizmo.attach(c.obj);
    else this.gizmo.detach();
    this.updateLooks();
  }

  setGizmoMode(mode) {
    this.gizmo.setMode(mode);
    // Move: only along the normal (moving inside the plane changes nothing). Rotate: only tilt.
    this.gizmo.showX = this.gizmo.showY = mode === 'rotate';
    this.gizmo.showZ = mode === 'translate';
    this.render();
  }

  setSnap(on) {
    this.gizmo.setTranslationSnap(on ? 1 : null);
    this.gizmo.setRotationSnap(on ? THREE.MathUtils.degToRad(5) : null);
  }

  setEnabled(on) {
    this.gizmo.enabled = on;
  }

  setPlaneOf(c, plane) {
    c.obj.position.copy(vec(plane.origin));
    c.obj.quaternion.copy(planeQuaternion(plane));
  }

  setPlane(id, plane) {
    this.setPlaneOf(this.cuts.get(id), plane);
    this.updateLooks();
  }

  /** The plane as the API wants it: origin, unit normal and in-plane x axis. */
  getPlane(id) {
    const o = this.cuts.get(id).obj;
    const axis = (x, y, z) => new THREE.Vector3(x, y, z).applyQuaternion(o.quaternion).toArray().map(tidy);
    return { origin: o.position.toArray().map(tidy), normal: axis(0, 0, 1), x_axis: axis(1, 0, 0) };
  }

  /** Position (mm) and rotation (degrees, X-Y-Z order) for the number fields. */
  getPose(id) {
    const o = this.cuts.get(id).obj;
    return { position: o.position.toArray(), rotation: [o.rotation.x, o.rotation.y, o.rotation.z].map(THREE.MathUtils.radToDeg) };
  }

  setPose(id, position, rotation) {
    const o = this.cuts.get(id).obj;
    o.position.fromArray(position);
    o.rotation.set(...rotation.map(THREE.MathUtils.degToRad));
    this.updateLooks();
  }

  /** Size every plane to cover the model, highlight the selected one, and update the model tint. */
  updateLooks() {
    if (!this.bounds) return;
    const pad = 0.05 * this.size + 2;
    for (const c of this.cuts.values()) {
      c.obj.updateMatrixWorld();
      const toLocal = c.obj.matrixWorld.clone().invert(), box = new THREE.Box3();
      for (let i = 0; i < 8; i++) {
        const b = this.bounds;
        box.expandByPoint(new THREE.Vector3(i & 1 ? b.max.x : b.min.x, i & 2 ? b.max.y : b.min.y,
          i & 4 ? b.max.z : b.min.z).applyMatrix4(toLocal));
      }
      const w = box.max.x - box.min.x + 2 * pad, h = box.max.y - box.min.y + 2 * pad;
      const cx = (box.min.x + box.max.x) / 2, cy = (box.min.y + box.max.y) / 2;
      c.rect.position.set(cx, cy, 0);
      c.rect.scale.set(w, h, 1);
      const len = 0.12 * this.size;              // the arrow marks the arrow side (see placeMarks)
      c.arrow.setLength(len, 0.35 * len, 0.22 * len);
      const on = c.id === this.selected;
      c.fill.material.opacity = on ? 0.24 : 0.1;
      c.edge.material.opacity = on ? 1 : 0.5;
    }
    this.updateTint();
    this.render();
  }

  updateTint() {
    const t = this.tint, ids = this.order.filter((id) => this.cuts.has(id));
    t.cutCount.value = ids.length;
    ids.forEach((id, i) => {
      const o = this.cuts.get(id).obj;
      t.cutOrigin.value[i].copy(o.position);
      t.cutNormal.value[i].set(0, 0, 1).applyQuaternion(o.quaternion);
    });
    // Cells that hold material, in the order of their sides: the same order as the part letters A, B, C...
    const s = this.samples || [], seen = new Set();
    for (let k = 0; k < s.length; k += 3) {
      let key = 0;
      for (let i = 0; i < ids.length; i++) {
        const o = t.cutOrigin.value[i], n = t.cutNormal.value[i];
        key = key * 2 + ((s[k] - o.x) * n.x + (s[k + 1] - o.y) * n.y + (s[k + 2] - o.z) * n.z < 0 ? 1 : 0);
      }
      seen.add(key);
    }
    const white = new THREE.Color('#ffffff'), ranks = [...seen].sort((a, b) => a - b);
    t.cellColor.value.forEach((color, key) => {
      const rank = ranks.indexOf(key);              // pale part colors, so the amber flange stands out
      if (rank < 0) color.set(MODEL_GRAY);
      else color.set(PART_COLORS[rank % PART_COLORS.length]).lerp(white, 0.6);
    });
  }

  // ------------------------------------------------------------ preview overlays
  /** Draw (or remove, with null) the preview of one cut. A cut with an error shows no overlay. */
  setOverlay(id, preview) {
    const old = this.overlays.get(id);
    if (old) {
      this.overlayGroup.remove(old);
      dispose(old);
      this.overlays.delete(id);
    }
    if (preview && !preview.error && this.cuts.has(id)) {
      const g = buildOverlay(preview, this.size);
      this.overlays.set(id, g);
      this.overlayGroup.add(g);
    }
    this.render();
  }

  /** Fade a cut's overlay while it no longer matches its plane (the plane moved, the preview is on its way). */
  setStale(id, stale) {
    this.overlays.get(id)?.traverse((o) => {
      const m = o.material;
      if (!m) return;
      m.userData.base ??= { opacity: m.opacity, transparent: m.transparent };
      m.transparent = stale || m.userData.base.transparent;
      m.opacity = stale ? 0.25 * m.userData.base.opacity : m.userData.base.opacity;
      m.needsUpdate = true;
    });
    this.render();
  }

  // ------------------------------------------------------------ parts after Generate
  /** The parts, loaded from their print-position STLs and put back where they are in the model. */
  setParts(parts, buffers) {
    this.clearParts();
    const loader = new STLLoader();
    parts.forEach((p, i) => {
      const color = PART_COLORS[i % PART_COLORS.length];
      const geometry = loader.parse(buffers[i]);
      const mesh = new THREE.Mesh(geometry, new THREE.MeshStandardMaterial({ color, roughness: 0.65, side: THREE.DoubleSide }));
      mesh.matrixAutoUpdate = false;
      mesh.matrix.set(...p.to_world);              // row-major 4x4: print position -> model
      geometry.computeBoundingBox();
      const label = badge(p.name, color);
      label.position.copy(geometry.boundingBox.getCenter(new THREE.Vector3()).applyMatrix4(mesh.matrix));
      const holder = new THREE.Group();
      holder.add(mesh, label);
      holder.userData.explode = vec(p.explode);
      this.partGroup.add(holder);
    });
    this.setExplode(0);
  }

  clearParts() {
    for (const holder of [...this.partGroup.children]) {
      this.partGroup.remove(holder);
      dispose(holder);
    }
  }

  /** Move every part away from the others along its own direction; f = 0 (together) to 1. */
  setExplode(f) {
    for (const h of this.partGroup.children) h.position.copy(h.userData.explode).multiplyScalar(f * 0.35 * this.size);
    this.render();
  }

  /** Switch between editing (model and cuts) and the parts. */
  showParts(on) {
    this.showingParts = on;
    if (this.model) this.model.visible = !on;
    this.cutGroup.visible = this.overlayGroup.visible = !on;
    this.partGroup.visible = on;
    this.select(this.selected);
  }

  // ------------------------------------------------------------ mouse
  listenForClicks() {
    const canvas = this.renderer.domElement;
    let down = null;
    canvas.addEventListener('pointerdown', (e) => {              // the gizmo saw this press first
      down = { x: e.clientX, y: e.clientY, gizmo: this.gizmo.dragging || this.gizmo.axis !== null };
    });
    canvas.addEventListener('pointerup', (e) => {
      const click = down && !down.gizmo && e.button === 0 && Math.hypot(e.clientX - down.x, e.clientY - down.y) < 5;
      down = null;
      if (!click || this.showingParts) return;
      const r = canvas.getBoundingClientRect();
      const p = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, 1 - ((e.clientY - r.top) / r.height) * 2);
      this.raycaster.setFromCamera(p, this.camera);
      if (e.shiftKey) {                            // Shift+click on the model: move the selected cut there
        const hit = this.model && this.raycaster.intersectObject(this.model)[0];
        if (hit && this.selected) this.events.pickPoint(hit.point.toArray());
        return;
      }
      const targets = [...this.cuts.values()].flatMap((c) => [c.fill, c.label]);
      const hits = this.raycaster.intersectObjects(targets);
      if (!hits.length) return;
      // A number badge is drawn on top of everything: a click on one selects that cut.
      const badge = hits.find((h) => h.object.isSprite);
      if (badge) {
        this.events.select(badge.object.userData.cutId);
        return;
      }
      // Cuts cross each other, often at the model center right under the gizmo. Near the gizmo, or
      // where the selected plane is hit next to the first plane, the selected cut stays selected, so a
      // slightly missed handle never switches cuts. Anywhere else the plane in front is selected.
      const own = hits.find((h) => h.object.userData.cutId === this.selected);
      if (own && (own === hits[0] || this.nearGizmo(e.clientX, e.clientY)
                  || own.point.distanceTo(hits[0].point) < 0.02 * this.size)) return;
      this.events.select(hits[0].object.userData.cutId);
    });
  }

  /** True when a page point lies within the selected cut's gizmo: its handles reach about this far. */
  nearGizmo(x, y) {
    const c = this.cuts.get(this.selected);
    if (!c) return false;
    const r = this.renderer.domElement.getBoundingClientRect();
    const p = c.obj.getWorldPosition(new THREE.Vector3()).project(this.camera);
    const gx = r.left + (p.x + 1) / 2 * r.width, gy = r.top + (1 - p.y) / 2 * r.height;
    return Math.hypot(x - gx, y - gy) < 0.12 * Math.min(r.width, r.height);
  }
}
