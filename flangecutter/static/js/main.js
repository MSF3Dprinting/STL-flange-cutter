// Flange Cutter page: the app state, and the wiring between the 3D view, the step panel,
// the 2D cut view and the API (see api.js). Every cut has all its own settings.
import * as api from './api.js';
import { Viewer, niceFrame, CUT_COLORS, PART_COLORS } from './viewer.js';
import * as panel from './panel.js';
import { drawCutView } from './cutview.js';

const $ = (id) => document.getElementById(id);
const state = {
  catalog: null,          // GET /api/bolts: bolt sizes, fastenings, parameter ranges and help texts
  prefs: null,            // the last used settings (all but the plane): the start for a new model
  model: null,            // answer of POST /api/model: model_id, name, sha256, bbox, warnings...
  cuts: [],               // {id, version, shown, suggested, plane, home, side, bolt, fastening, nut_pocket_half, distribution, params}
  sel: 0,                 // index of the selected cut
  preview: new Map(),     // cut id -> the latest preview of that cut
  previewWarnings: [],    // warnings about several cuts together
  notices: [],            // e.g. "these settings were saved for another model"
  result: null,           // answer of POST /api/generate
  busy: false,            // opening a model or generating: the forms are locked
};
window.flangeCutter = { state };      // for the UI test, and for debugging in the browser console
let viewer, nextId = 1;

function status(text, kind = 'info') {
  const e = $('status-text');
  e.textContent = e.title = text;     // the tooltip shows all of a long message
  e.className = kind;
}

// ------------------------------------------------------------ settings of a cut
function factorySettings() {
  const c = state.catalog;
  return {
    side: c.defaults.side, bolt: c.defaults.bolt, fastening: c.defaults.fastening,
    nut_pocket_half: c.defaults.nut_pocket_half,
    distribution: { mode: 'count', count: c.count.default, spacing: c.spacing.default },
    params: Object.fromEntries(c.params.map((p) => [p.key, p.default])),
  };
}

const within = (v, lo, hi) => typeof v === 'number' && v >= lo && v <= hi;

/** All but the plane, from a file or the preferences. Anything unknown or out of range gets the default;
 *  `replaced` collects the names of the values that were there but not valid. */
function cleanSettings(raw, replaced = []) {
  const c = state.catalog, out = factorySettings();
  if (!raw || typeof raw !== 'object') {
    replaced.push('settings');
    return out;
  }
  const keep = (value, ok, name) => {          // use this value? A missing one gets the default without a word
    if (!ok && value !== undefined) replaced.push(name);
    return ok;
  };
  const d = raw.distribution || {}, o = out.distribution;
  if (keep(raw.side, ['outside', 'inside'].includes(raw.side), 'flange side')) out.side = raw.side;
  if (keep(raw.bolt, c.bolts.some((b) => b.name === raw.bolt), 'bolt size')) out.bolt = raw.bolt;
  if (keep(raw.fastening, c.fastenings.some((f) => f.key === raw.fastening), 'fastening')) out.fastening = raw.fastening;
  if (keep(raw.nut_pocket_half, ['A', 'B'].includes(raw.nut_pocket_half), 'nut pocket side')) out.nut_pocket_half = raw.nut_pocket_half;
  if (keep(d.mode, ['count', 'spacing'].includes(d.mode), 'bolt layout')) o.mode = d.mode;
  if (keep(d.count, Number.isInteger(d.count) && within(d.count, c.count.min, c.count.max), 'number of bolts')) o.count = d.count;
  if (keep(d.spacing, within(d.spacing, c.spacing.min, c.spacing.max), 'bolt spacing')) o.spacing = d.spacing;
  for (const p of c.params) {                  // empty (null) = the automatic or standard value
    const v = raw.params?.[p.key];
    if (keep(v, v === null || within(v, p.min, p.max), p.label.toLowerCase()) && v !== null) out.params[p.key] = v;
  }
  return out;
}

const isVector = (a) => Array.isArray(a) && a.length === 3 && a.every(Number.isFinite);

function cleanPlane(raw) {
  if (!raw || !isVector(raw.origin) || !isVector(raw.normal) || Math.hypot(...raw.normal) < 1e-9) return null;
  return { origin: [...raw.origin], normal: [...raw.normal],
    x_axis: isVector(raw.x_axis) ? [...raw.x_axis] : niceFrame(raw.normal).x_axis };
}

const selCut = () => state.cuts[state.sel];

function settingsOf(cut) {
  const { side, bolt, fastening, nut_pocket_half, distribution, params } = cut;
  return structuredClone({ side, bolt, fastening, nut_pocket_half, distribution, params });
}

const apiCut = (cut) => ({ plane: cut.plane, ...settingsOf(cut) });

/** version counts the changes of a cut; shown is the version its displayed preview was made for;
 *  suggested is the bolt count suggested by its last preview without an error. */
function makeCut(settings, plane) {
  return { id: nextId++, version: 0, shown: -1, suggested: null, plane, home: structuredClone(plane), ...structuredClone(settings) };
}

// ------------------------------------------------------------ planes
const center = () => state.model.bbox.center;
const dot = (a, b) => a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
const cross = (a, b) => [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
const reach = (n) => dot(n.map(Math.abs), state.model.bbox.size) / 2;   // half the model's extent along n

/** A plane with this normal through the model center, or `offset` mm from it along the normal. */
function planeAt(normal, offset = 0) {
  const frame = niceFrame(normal);
  return { origin: center().map((v, i) => v + offset * frame.normal[i]), ...frame };
}

const axisPlane = (axis) => planeAt([0, 1, 2].map((k) => (k === axis ? 1 : 0)));   // "Across X/Y/Z"

/** Across the longest side: models are mostly cut to fit the printer. A plane through the center can
 *  miss the model (two cups side by side): then across the next longest side. */
function defaultPlane() {
  const s = state.model.bbox.size, planes = [0, 1, 2].sort((a, b) => s[b] - s[a]).map(axisPlane);
  return planes.find((p) => viewer.crosses(p)) || planes[0];
}

/** The plane of a new cut: turned 90° from the selected cut (normal = its in-plane y, then x axis) or
 *  parallel to it; through the model center, or a quarter of the model away. Of those that go through
 *  the model: the first one that is not parallel to a cut (within 15°), else the farthest from them. */
function newPlane(src) {
  const { normal: n, x_axis: x } = src.plane;
  let best = planeAt(cross(n, x)), room = -1;
  for (const k of [0, 0.5, -0.5]) {
    for (const normal of [cross(n, x), x, n]) {
      const p = planeAt(normal, k * reach(normal));
      const gap = Math.min(...state.cuts.map((c) => (Math.abs(dot(c.plane.normal, p.normal)) > 0.96
        ? Math.abs(dot(p.normal, p.origin.map((v, i) => v - c.plane.origin[i]))) : Infinity)));
      if (gap > room && viewer.crosses(p)) [best, room] = [p, gap];
    }
  }
  return best;
}

function shiftInfo(cut) {             // the "Shift along normal" slider: distance from the model center
  const n = cut.plane.normal, r = Math.ceil(reach(n) + 5);
  return { min: -r, max: r, value: dot(n, cut.plane.origin.map((v, i) => v - center()[i])) };
}

function moveOrigin(cut, origin) {    // keeps the rotation fields exactly as they are
  viewer.setPose(cut.id, origin, viewer.getPose(cut.id).rotation);
  planeChanged(cut);
}

function syncViewer() {
  viewer.setCuts(state.cuts.map((c) => ({ id: c.id, plane: c.plane })), selCut()?.id);
  for (const c of state.cuts) c.plane = viewer.getPlane(c.id);   // tidied: unit normal, x axis in the plane
}

// ------------------------------------------------------------ showing the state
function rowOf(cut, i) {
  const p = state.preview.get(cut.id), d = cut.distribution;
  const bolts = p && !p.error ? `${p.count} bolts` : d.mode === 'count' ? `${d.count} bolts` : `every ${d.spacing} mm`;
  const [kind, text] = !p ? ['', '...'] : p.error ? ['error', 'Error'] : p.warnings.length ? ['warn', 'Warning'] : ['ok', 'OK'];
  return { number: i + 1, color: CUT_COLORS[i], summary: `${cut.bolt} · ${bolts} · ${cut.side}`, status: kind,
    statusText: text, stale: cut.shown !== cut.version, title: p?.error || p?.warnings?.map((w) => w.message).join('\n') || null };
}

/** A cut's own error or warnings, from its preview. */
const notesOf = (p) => (p?.error ? [{ message: p.error, action: '', error: true }] : p?.warnings || []);

function warnings() {
  if (state.result) return [...state.notices, ...state.result.warnings];
  return [...state.notices, ...state.cuts.flatMap((c) => notesOf(state.preview.get(c.id))), ...state.previewWarnings];
}

function drawCut() {
  const cut = selCut(), p = cut && state.preview.get(cut.id);
  $('cutview-title').textContent = cut ? `Cut ${state.sel + 1}, seen from the arrow side` : '2D cut view';
  $('cutview-msg').textContent = p?.error || '';
  $('cutview-svg').classList.toggle('stale', !!cut && cut.shown !== cut.version);
  const marks = drawCutView($('cutview-svg'), p && !p.error ? p : null, CUT_COLORS);
  $('key-noroom').hidden = !marks.noRoom;       // the legend explains only what is drawn
  $('key-blocked').hidden = !marks.blocked;
}

/** Update everything that shows the cuts: list, forms, warnings, results, 2D view, buttons. */
function refresh() {
  panel.setEnabled(!!state.model, state.busy);
  viewer.setEnabled(!state.busy);
  $('empty-hint').hidden = !!state.model;
  $('show-parts').hidden = !state.result || viewer.showingParts;
  $('result-tools').hidden = !viewer.showingParts;
  const cut = selCut();
  if (!cut) return;
  const p = state.preview.get(cut.id), errors = state.cuts.map((c) => state.preview.get(c.id)?.error).filter(Boolean);
  panel.showCutList(state.cuts.map(rowOf), state.sel, state.catalog.max_cuts);
  panel.showCutNotes(notesOf(p), cut.shown !== cut.version);
  panel.showCut(cut, state.sel + 1, CUT_COLORS[state.sel], p);
  panel.showPose(viewer.getPose(cut.id), shiftInfo(cut));
  panel.showWarnings(warnings(), !state.result && state.cuts.some((c) => c.shown !== c.version));
  panel.showGenerate(errors[0] || '');
  panel.showResults(state.result, PART_COLORS);
  drawCut();
}

// ------------------------------------------------------------ preview
let previewTimer = 0, sent = 0, shown = 0, prefsTimer = 0;

function schedulePreview(ms = 200) {
  clearTimeout(previewTimer);
  previewTimer = setTimeout(runPreview, ms);
}

/** Show a preview answer (also the one inside a Generate answer) for the cut versions that were sent. */
function applyPreview(asked, res) {
  asked.forEach(({ cut, version }, i) => {
    if (!state.cuts.includes(cut)) return;          // deleted meanwhile
    const p = res.cuts[i];
    state.preview.set(cut.id, p);
    cut.shown = version;
    if (!p.error && p.suggested_count > 0) cut.suggested = p.suggested_count;   // kept while the cut has an error
    viewer.setOverlay(cut.id, p);
    viewer.setStale(cut.id, cut.version !== version);
  });
  state.previewWarnings = res.warnings;
}

/** Always sends ALL cuts (they affect each other); answers that arrive late are ignored. */
async function runPreview() {
  if (!state.model || !state.cuts.length) return;
  if (state.busy) return schedulePreview(500);
  const seq = ++sent, model = state.model;
  const asked = state.cuts.map((cut) => ({ cut, version: cut.version }));
  try {
    const res = await api.postJSON('/api/preview', { model_id: model.model_id, cuts: state.cuts.map(apiCut) });
    if (seq < shown || model !== state.model) return;
    shown = seq;
    applyPreview(asked, res);
    // a model in the wrong unit is the real cause of most cut errors: say that first
    const bad = res.cuts.find((c) => c.error), unit = model.warnings.find((w) => w.code?.startsWith('units'));
    if (bad) status(unit ? `${unit.message} ${bad.error}` : bad.error, 'error');
    else if ($('status-text').className === 'error') status(unit ? unit.message : 'Preview updated.', unit ? 'warn' : 'info');
  } catch (e) {
    if (seq >= shown) status(e.message, 'error');
  }
  refresh();
}

/** A cut changed: its overlay fades until the new preview arrives, and parts made before are dropped. */
function edited(cut, settings = false) {
  cut.version++;
  viewer.setStale(cut.id, true);
  dropResult();
  if (settings) savePrefsSoon(cut);
  schedulePreview();
}

function planeChanged(cut) {
  cut.plane = viewer.getPlane(cut.id);
  edited(cut);
  refresh();
}

function dropResult() {
  if (!state.result) return;
  state.result = null;
  viewer.clearParts();
  viewer.showParts(false);
  status('The cuts changed. Click Generate to make new parts.', 'warn');
}

function savePrefsSoon(cut) {          // the selected cut's settings become the start for the next model
  state.prefs = settingsOf(cut);
  clearTimeout(prefsTimer);
  prefsTimer = setTimeout(() => api.postJSON('/api/prefs', state.prefs).catch(() => {}), 800);
}

// ------------------------------------------------------------ user actions
async function openFile(file) {
  if (!file || state.busy) return;
  if (!/\.stl$/i.test(file.name)) return status('This is not an STL file. Choose a file that ends in .stl.', 'error');
  state.busy = true;
  refresh();
  status(`Opening ${file.name}...`);
  try {
    const info = await api.uploadModel(file);
    const buffer = await api.getBuffer(`/api/model/${info.model_id}/mesh.stl`);
    Object.assign(state, { model: info, result: null, notices: [], previewWarnings: [], sel: 0 });
    state.preview.clear();
    viewer.setModel(buffer, info.bbox);
    viewer.setSeeThrough($('see-through').checked);     // the new model looks as the check box says
    state.cuts = [makeCut(state.prefs, defaultPlane())];
    syncViewer();
    panel.showModel(info);
    schedulePreview(0);
    const w = info.warnings[0];
    status(w ? w.message : 'Model opened. Place the cut, then click Generate.', w ? 'warn' : 'info');
  } catch (e) {
    panel.showModelError(`${file.name}: ${e.message}`);
    status(e.message, 'error');
  } finally {
    state.busy = false;
    refresh();
  }
}

const actions = {
  openFile,
  selectCut(i) {
    state.sel = i;
    viewer.select(selCut().id);
    refresh();
  },
  addCut() {                           // the same settings as the selected cut, on a plane of its own
    if (state.cuts.length >= state.catalog.max_cuts) return;
    const cut = makeCut(settingsOf(selCut()), newPlane(selCut()));
    state.cuts.push(cut);
    state.sel = state.cuts.length - 1;
    syncViewer();
    edited(cut);
    refresh();
  },
  deleteCut() {
    if (state.cuts.length <= 1 || !window.confirm(`Delete cut ${state.sel + 1}?`)) return;
    const [cut] = state.cuts.splice(state.sel, 1);
    state.preview.delete(cut.id);
    state.sel = Math.min(state.sel, state.cuts.length - 1);
    syncViewer();
    for (const c of state.cuts) {      // new numbers, maybe other bolts: the old previews are out of date
      c.version++;
      viewer.setStale(c.id, true);
    }
    dropResult();
    schedulePreview();
    refresh();
  },
  preset(name) {
    const cut = selCut();
    if (name === 'center') return moveOrigin(cut, [...center()]);
    viewer.setPlane(cut.id, name === 'reset' ? cut.home : axisPlane('xyz'.indexOf(name)));
    planeChanged(cut);
  },
  setMode(mode) {
    viewer.setGizmoMode(mode);
    panel.setMode(mode);
  },
  setSnap: (on) => viewer.setSnap(on),
  setPose(position, rotation) {        // null = keep that part exactly as it is
    const cut = selCut(), now = viewer.getPose(cut.id);
    viewer.setPose(cut.id, position || now.position, rotation || now.rotation);
    planeChanged(cut);
  },
  shift(distance) {
    const cut = selCut(), n = cut.plane.normal, now = shiftInfo(cut).value;
    moveOrigin(cut, cut.plane.origin.map((v, i) => v + (distance - now) * n[i]));
  },
  change(patch) {                      // e.g. {bolt: 'M5'} or {params: {thickness: null}}
    const cut = selCut();
    for (const [k, v] of Object.entries(patch)) {
      if (v && typeof v === 'object') Object.assign(cut[k], v);
      else cut[k] = v;
    }
    edited(cut, true);
    refresh();
  },
  suggest() {
    const n = selCut().suggested;
    if (n) actions.change({ distribution: { mode: 'count', count: n } });
  },
  resetDefaults() {
    const cut = selCut();
    Object.assign(cut, factorySettings());
    edited(cut, true);
    refresh();
  },
  applyAll() {
    const src = selCut(), others = state.cuts.length - 1;
    if (!others || !window.confirm(`Copy the flange side, bolts and flange of cut ${state.sel + 1} to the other `
      + `${others === 1 ? 'cut' : `${others} cuts`}? Their own settings are replaced.`)) return;
    for (const c of state.cuts) {
      if (c === src) continue;
      Object.assign(c, settingsOf(src));
      edited(c);
    }
    status(`All cuts now use the settings of cut ${state.sel + 1}.`);
    refresh();
  },
  generate,
  saveSettings,
  loadSettings,
};

async function generate() {
  if (panel.hasInvalid()) return status('Correct the fields marked in red first.', 'error');
  clearTimeout(previewTimer);
  state.busy = true;
  refresh();
  status('Generating the parts...');
  panel.showProgress('Starting', 0);
  const poll = setInterval(async () => {
    try {
      const p = await api.getJSON('/api/progress');
      if (p.busy && state.busy) panel.showProgress(p.stage, p.fraction);
    } catch { /* the answer to Generate tells what went wrong */ }
  }, 300);
  const seq = ++sent, asked = state.cuts.map((cut) => ({ cut, version: cut.version }));
  try {
    const result = await api.postJSON('/api/generate', { model_id: state.model.model_id, cuts: state.cuts.map(apiCut) });
    if (seq >= shown) {                // it has the preview of every cut: an edit just before is shown too
      shown = seq;
      applyPreview(asked, result.preview);
    }
    panel.showProgress('Loading the parts', 0.95);
    const buffers = await Promise.all(result.parts.map((p) => api.getBuffer(p.url)));
    state.result = result;
    viewer.setParts(result.parts, buffers);
    viewer.showParts(true);
    $('explode').value = 0;
    status(`Done: ${result.parts.length} parts. Download them in step 5.`);
  } catch (e) {
    status(e.message, 'error');
  } finally {
    clearInterval(poll);
    panel.showProgress(null);
    state.busy = false;
    if (state.cuts.some((c) => c.shown !== c.version)) schedulePreview(0);   // Generate failed after an edit
    refresh();
  }
  if (state.result) $('results').scrollIntoView({ block: 'nearest' });
}

/** The same content as settings.json in the ZIP: the cut objects this page sends, plus the model. */
function saveSettings() {
  const m = state.model;
  const data = { format: 1, app: 'Flange Cutter', version: state.catalog.version,
    model: { name: m.name, sha256: m.sha256 }, cuts: state.cuts.map(apiCut) };
  const a = document.createElement('a');
  a.href = URL.createObjectURL(new Blob([JSON.stringify(data, null, 1)], { type: 'application/json' }));
  a.download = `${m.name.replace(/\.stl$/i, '')}_settings.json`;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 10000);
  status(`Settings saved as ${a.download}.`);
}

async function loadSettings(file) {
  if (!file) return;
  let data = null;
  try {
    data = JSON.parse(await file.text());
  } catch { /* not JSON */ }
  if (data?.format !== 1 || !Array.isArray(data.cuts) || !data.cuts.length) {
    return status('This is not a Flange Cutter settings file.', 'error');
  }
  const max = state.catalog.max_cuts, replaced = [];
  state.notices = [];
  if (data.model?.sha256 && data.model.sha256 !== state.model.sha256) {
    state.notices.push({ message: `These settings were saved for the model "${data.model.name}".`,
      action: 'They were used anyway. Check every cut before you generate.' });
  }
  if (data.cuts.length > max) {
    state.notices.push({ message: `The file has ${data.cuts.length} cuts. Only the first ${max} were loaded.`, action: '' });
  }
  state.cuts = data.cuts.slice(0, max).map((c, i) => {
    const bad = [], plane = cleanPlane(c?.plane), cut = makeCut(cleanSettings(c, bad), plane || defaultPlane());
    if (!plane) bad.push('plane');
    replaced.push(...bad.map((name) => `cut ${i + 1} ${name}`));
    return cut;
  });
  if (replaced.length) {
    state.notices.push({ message: `Some values in the file were not valid and were replaced: ${replaced.join(', ')}.`,
      action: 'The standard values are used instead. Check these cuts before you generate.' });
  }
  state.sel = 0;
  state.preview.clear();
  dropResult();
  syncViewer();
  schedulePreview(0);
  const n = state.notices[0];
  status(n ? n.message : `Settings loaded from ${file.name}.`, n ? 'warn' : 'info');
  refresh();
}

async function quit() {
  if (state.model && !window.confirm('Quit Flange Cutter? Download your parts and save your settings first.')) return;
  try {
    await api.postJSON('/api/shutdown', {});
  } catch { /* it has stopped already */ }
  clearTimeout(previewTimer);
  const bye = document.createElement('div');       // covers the page; late answers can still update it safely
  bye.className = 'bye';
  bye.textContent = 'You can close this tab now.';
  document.body.append(bye);
}

// ------------------------------------------------------------ 3D view events and page wiring
const viewerEvents = {
  select(id) {
    const i = state.cuts.findIndex((c) => c.id === id);
    if (i >= 0 && !state.busy) actions.selectCut(i);
  },
  planeMoved(id, dragging) {           // the gizmo moves a cut: preview when the drag ends
    const cut = state.cuts.find((c) => c.id === id);
    if (!cut) return;
    if (!dragging) return planeChanged(cut);
    cut.plane = viewer.getPlane(id);
    cut.version++;
    viewer.setStale(id, true);
    dropResult();
    panel.showPose(viewer.getPose(id), shiftInfo(cut));
    $('cutview-svg').classList.toggle('stale', cut === selCut());
  },
  pickPoint(point) {                   // Shift+click on the model
    if (selCut() && !state.busy) moveOrigin(selCut(), point);
  },
};

function wire() {
  $('fit-view').onclick = () => viewer.fitView();
  $('see-through').onchange = (e) => viewer.setSeeThrough(e.target.checked);
  $('explode').addEventListener('input', (e) => viewer.setExplode(e.target.value / 100));
  $('back-to-edit').onclick = () => {
    viewer.showParts(false);
    refresh();
  };
  $('show-parts').onclick = () => {
    viewer.showParts(true);
    refresh();
  };
  $('quit').onclick = quit;
  window.addEventListener('keydown', (e) => {          // W = move, E = rotate, unless typing in a field
    if (e.ctrlKey || e.altKey || e.metaKey || e.target.matches('input[type=number], input[type=text], textarea, select')) return;
    const mode = { w: 'translate', e: 'rotate' }[e.key.toLowerCase()];
    if (mode && state.model) actions.setMode(mode);
  });
  // drag and drop an STL file onto the 3D view; never let the browser open a dropped file itself
  const view = $('view3d');
  const dropHint = (on) => {
    view.classList.toggle('dragover', on);
    $('drop-hint').hidden = !on;
  };
  for (const type of ['dragover', 'drop']) window.addEventListener(type, (e) => e.preventDefault());
  view.addEventListener('dragover', () => dropHint(true));
  view.addEventListener('dragleave', (e) => !view.contains(e.relatedTarget) && dropHint(false));
  view.addEventListener('drop', (e) => {
    dropHint(false);
    openFile(e.dataTransfer.files[0]);
  });
  new ResizeObserver(drawCut).observe($('cutview-svg'));
}

async function start() {
  try {
    state.catalog = await api.getJSON('/api/bolts');
  } catch (e) {
    return status(e.message, 'error');
  }
  state.prefs = cleanSettings(await api.getJSON('/api/prefs').catch(() => ({})));
  $('version').textContent = state.catalog.version;
  try {
    viewer = new Viewer($('view3d'), viewerEvents);
  } catch {
    return status('This browser cannot show 3D views. Open the app in Microsoft Edge or Google Chrome.', 'error');
  }
  window.flangeCutter.viewer = viewer;
  panel.initPanel(state.catalog, actions);
  wire();
  refresh();
  status('Open an STL file to start.');
  document.body.dataset.ready = '1';
}

start();
