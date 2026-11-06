// Step panel: model facts, the list of cuts, the forms for the selected cut, warnings and results.
// It never talks to the server: main.js passes data in, and user actions go out through `actions`.
const $ = (id) => document.getElementById(id);
const POSITION = ['pos-x', 'pos-y', 'pos-z'], ROTATION = ['rot-x', 'rot-y', 'rot-z'];
let catalog, actions;
const invalid = new Set();          // ids of the fields that hold a value the app cannot use

/** Make an element: h('td', {class: 'x'}, 'text', childElement...). */
function h(tag, attrs = {}, ...children) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) if (v != null) e.setAttribute(k, v);
  e.append(...children.filter((c) => c != null));
  return e;
}

/** A number for display: at most `digits` decimals, no trailing zeros. */
export const fmt = (v, digits = 1) => (v == null ? '' : String(+Number(v).toFixed(digits) + 0));
const rangeText = (min, max, unit) => (unit === 'deg' ? `${min}–${max}°` : `${min}–${max} ${unit}`);
const many = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

export function initPanel(cat, act) {
  catalog = cat;
  actions = act;
  $('bolt-sizes').append(...cat.bolts.map((b) => radio('bolt', b.name, b.name,
    `${b.name}: hole ${b.hole} mm, washer ${b.washer} mm, nut ${b.nut_af} mm across flats`)));
  $('fastenings').append(...cat.fastenings.map((f) => radio('fastening', f.key, f.label, f.help)));
  for (const p of cat.params) (p.advanced ? $('adv-params') : $('main-params')).append(numberField(p));
  $('count-range').textContent = `${cat.count.min}–${cat.count.max}`;
  $('spacing-range').textContent = rangeText(cat.spacing.min, cat.spacing.max, 'mm');
  wire();
}

function radio(name, value, label, title) {
  return h('label', { title }, h('input', { type: 'radio', name, value }), label);
}

/** Number fields are text fields, so that a decimal comma is not dropped (see check). */
function numberField(p) {
  const id = `p-${p.key}`;
  return h('div', { class: 'field', title: p.help },
    h('label', { for: id }, p.label, h('span', { class: 'range' }, rangeText(p.min, p.max, p.unit))),
    h('input', { type: 'text', inputmode: 'decimal', autocomplete: 'off', class: 'number', id }),
    h('span', { class: 'unit' }, p.unit === 'deg' ? '°' : p.unit));
}

/** The field's number; null when it is empty and may be; undefined when it is not valid (it turns red).
 *  A decimal comma counts as a point: 12,5 is 12.5. */
function check(input, min, max, { integer = false, optional = false } = {}) {
  const text = input.value.trim().replace(',', '.');
  let value = text === '' ? (optional ? null : undefined) : Number(text);
  if (value != null && (!(value >= min && value <= max) || (integer && !Number.isInteger(value)))) value = undefined;
  input.classList.toggle('invalid', value === undefined);
  input.setAttribute('aria-invalid', String(value === undefined));
  if (value === undefined) invalid.add(input.id);
  else invalid.delete(input.id);
  updateGenerate();
  return value;
}

export const hasInvalid = () => invalid.size > 0;

function onRadio(name, fn) {
  for (const r of document.querySelectorAll(`input[name="${name}"]`)) {
    r.addEventListener('change', () => r.checked && fn(r.value));
  }
}

function wire() {
  $('open-btn').onclick = () => $('file-input').click();
  $('file-input').onchange = (e) => {
    actions.openFile(e.target.files[0]);
    e.target.value = '';
  };
  $('add-cut').onclick = actions.addCut;
  $('delete-cut').onclick = actions.deleteCut;
  for (const b of document.querySelectorAll('[data-preset]')) b.onclick = () => actions.preset(b.dataset.preset);
  onRadio('mode', actions.setMode);
  $('snap').onchange = (e) => actions.setSnap(e.target.checked);
  for (const id of [...POSITION, ...ROTATION]) $(id).addEventListener('input', readPose);
  $('shift').addEventListener('input', (e) => actions.shift(Number(e.target.value)));
  onRadio('side', (v) => actions.change({ side: v }));
  onRadio('bolt', (v) => actions.change({ bolt: v }));
  onRadio('fastening', (v) => actions.change({ fastening: v }));
  onRadio('pocket', (v) => actions.change({ nut_pocket_half: v }));
  onRadio('dist', (v) => actions.change({ distribution: { mode: v } }));
  $('count').addEventListener('input', (e) => {
    const v = check(e.target, catalog.count.min, catalog.count.max, { integer: true });
    if (v !== undefined) actions.change({ distribution: { count: v } });
  });
  $('spacing').addEventListener('input', (e) => {
    const v = check(e.target, catalog.spacing.min, catalog.spacing.max);
    if (v !== undefined) actions.change({ distribution: { spacing: v } });
  });
  for (const p of catalog.params) {
    $(`p-${p.key}`).addEventListener('input', (e) => {
      const v = check(e.target, p.min, p.max, { optional: true });   // empty = automatic or default value
      if (v !== undefined) actions.change({ params: { [p.key]: v } });
    });
  }
  $('suggest').onclick = () => {
    invalid.delete('count');          // the suggestion replaces a red entry
    actions.suggest();
  };
  $('reset-defaults').onclick = () => {
    invalid.clear();                  // the defaults replace the red entries too
    actions.resetDefaults();
  };
  $('apply-all').onclick = actions.applyAll;
  $('generate').onclick = actions.generate;
  $('save-settings').onclick = actions.saveSettings;
  $('load-settings').onclick = () => $('settings-input').click();
  $('settings-input').onchange = (e) => {
    actions.loadSettings(e.target.files[0]);
    e.target.value = '';
  };
  document.addEventListener('keydown', (e) => {     // arrow keys step a field with data-step, as in a number input
    const input = e.target, step = Number(input.dataset?.step);
    if (!step || (e.key !== 'ArrowUp' && e.key !== 'ArrowDown')) return;
    const text = input.value.trim().replace(',', '.');
    if (text === '' || !Number.isFinite(Number(text))) return;
    e.preventDefault();
    input.value = fmt(Number(text) + (e.key === 'ArrowUp' ? step : -step), 3);
    input.dispatchEvent(new Event('input'));
  });
}

/** Position or rotation typed in: only the group that was edited changes (the others show rounded values). */
function readPose(e) {
  const moved = POSITION.includes(e.target.id), ids = moved ? POSITION : ROTATION;
  const values = ids.map((id) => check($(id), moved ? -1e5 : -360, moved ? 1e5 : 360));
  if (!values.includes(undefined)) actions.setPose(moved ? values : null, moved ? null : values);
}

/** Put a value in a field, unless the user is typing in it. A red field keeps the user's entry
 *  (so it is not lost without notice) unless `force`: another cut was selected, or the field is hidden. */
function setField(input, value, force = true) {
  if (document.activeElement === input || (!force && invalid.has(input.id))) return;
  input.value = value ?? '';
  input.classList.remove('invalid');
  invalid.delete(input.id);
}

/** Check the radio button with this value. Its label gets the class "on": the chosen look (no CSS :has()). */
function setRadio(name, value) {
  for (const r of document.querySelectorAll(`input[name="${name}"]`)) {
    r.checked = r.value === value;
    r.parentElement.classList.toggle('on', r.checked);
  }
}

export function setEnabled(hasModel, busy) {
  for (const id of ['step-cuts', 'step-side', 'step-bolts', 'step-generate']) $(id).disabled = !hasModel || busy;
  $('open-btn').disabled = busy;
}

export function setMode(mode) {
  setRadio('mode', mode);
}

// ------------------------------------------------------------ step 1
export function showModel(info) {
  $('model-info').hidden = false;
  $('m-name').textContent = info.name;
  $('m-size').textContent = `${info.bbox.size.map((v) => fmt(v)).join(' × ')} mm`;
  $('m-tris').textContent = info.triangles.toLocaleString('en-US');
  $('m-check').textContent = info.repaired ? 'closed solid (small errors repaired)' : 'closed solid (watertight)';
  showList($('model-warnings'), info.warnings);
}

export function showModelError(message) {
  showList($('model-warnings'), [{ message, action: '', error: true }]);
}

// ------------------------------------------------------------ steps 2-4: the cuts
/** rows: [{number, color, summary, status: 'ok'|'warn'|'error'|'', statusText, stale, title}] */
export function showCutList(rows, selected, maxCuts) {
  const list = $('cut-list');                 // rows are updated in place, so a click is never lost
  while (list.children.length > rows.length) list.lastChild.remove();
  rows.forEach((r, i) => {
    let b = list.children[i]?.firstChild;
    if (!b) {
      b = h('button', { type: 'button' }, h('span', { class: 'swatch' }), h('span', { class: 'cut-name' }),
        h('span', { class: 'cut-sum' }), h('span', {}));
      b.onclick = () => actions.selectCut(i);
      list.append(h('li', {}, b));
    }
    const [swatch, name, sum, badge] = b.children;
    b.className = `cut-row${i === selected ? ' selected' : ''}`;
    b.title = r.title || '';
    b.setAttribute('aria-pressed', String(i === selected));
    swatch.style.background = r.color;
    name.textContent = `Cut ${r.number}`;
    sum.textContent = r.summary;
    badge.className = `badge ${r.status}${r.stale ? ' stale' : ''}`;
    badge.textContent = r.statusText;
  });
  $('add-cut').disabled = rows.length >= maxCuts;
  $('add-cut').title = rows.length >= maxCuts ? `You can use up to ${maxCuts} cuts.`
    : 'A new cut with the settings of the selected cut, turned 90° or set apart from the other cuts';
  $('delete-cut').disabled = $('apply-all').disabled = rows.length <= 1;
}

/** The selected cut's own error or warnings, right under the list of cuts. */
export function showCutNotes(items, stale) {
  showList($('cut-notes'), items);
  $('cut-notes').classList.toggle('stale', stale);
}

let shownCut = null;

/** The form of steps 3 and 4 for the selected cut; preview = its latest preview, for automatic values. */
export function showCut(cut, number, color, preview) {
  const fresh = cut.id !== shownCut;
  shownCut = cut.id;
  for (const e of document.querySelectorAll('.for-cut')) {     // steps 2-4 name the cut they change
    e.replaceChildren(h('span', { class: 'swatch', style: `background:${color}` }), `Cut ${number}`);
  }
  $('reset-defaults').title = `Flange side, bolts and flange of cut ${number} back to the standard values`;
  $('apply-all').title = `Copy the flange side, bolts and flange of cut ${number} to the other cuts`;
  setRadio('side', cut.side);
  setRadio('bolt', cut.bolt);
  setRadio('fastening', cut.fastening);
  setRadio('pocket', cut.nut_pocket_half);
  setRadio('dist', cut.distribution.mode);
  $('count-field').hidden = cut.distribution.mode !== 'count';
  $('spacing-field').hidden = cut.distribution.mode !== 'spacing';
  $('pocket-field').hidden = cut.fastening !== 'nut_pocket';
  setField($('count'), cut.distribution.count, fresh || cut.distribution.mode !== 'count');
  setField($('spacing'), cut.distribution.spacing, fresh || cut.distribution.mode !== 'spacing');
  const bolt = catalog.bolts.find((b) => b.name === cut.bolt);
  const d = preview && !preview.error ? preview.derived : bolt.auto;      // automatic values
  for (const p of catalog.params) {
    const input = $(`p-${p.key}`);
    setField(input, cut.params[p.key], fresh);
    const auto = p.default ?? d[p.key];
    input.placeholder = auto == null ? 'auto' : p.default == null ? `auto ${fmt(auto, 2)}` : fmt(auto, 2);
  }
  const suggested = cut.suggested;           // from the last preview without an error, e.g. before "too many bolts"
  $('suggest').disabled = !suggested;
  $('count-range').textContent = `${catalog.count.min}–${catalog.count.max}${suggested ? `, suggested ${suggested}` : ''}`;
}

/** Position, rotation and the shift slider of the selected cut. */
export function showPose(pose, shift) {
  POSITION.forEach((id, i) => setField($(id), fmt(pose.position[i], 2)));
  ROTATION.forEach((id, i) => setField($(id), fmt(pose.rotation[i], 1)));
  const s = $('shift');
  Object.assign(s, { min: shift.min, max: shift.max });
  if (document.activeElement !== s) s.value = shift.value;
  $('shift-out').textContent = `${fmt(shift.value)} mm`;
}

// ------------------------------------------------------------ step 5
function showList(ul, items) {
  ul.replaceChildren(...items.map((w) => h('li', { class: w.error ? 'error' : null },
    h('b', {}, w.message), w.action ? ` ${w.action}` : null)));
}

/** Warnings, each with what to do: [{message, action, error?}]. Faded while a preview is on its way. */
export function showWarnings(items, stale = false) {
  showList($('warnings'), items);
  $('warnings').classList.toggle('stale', stale);
  $('no-warnings').hidden = items.length > 0;
}

let cutError = '';

/** Generate runs only when no field is red and no cut has an error; the line under the button says why. */
export function showGenerate(error) {
  cutError = error;
  updateGenerate();
}

function updateGenerate() {
  const why = invalid.size ? 'Correct the fields marked in red first.' : cutError && `Fix this first: ${cutError}`;
  $('generate').disabled = !!why;
  $('generate-why').textContent = why;
}

export function showProgress(stage, fraction) {
  $('progress').hidden = stage == null;
  $('progress-stage').textContent = stage || '';
  $('progress-bar').value = fraction || 0;
}

let shownResult = null;

/** Parts, hardware and download buttons of a Generate result (or hide them, with null). */
export function showResults(result, partColors) {
  $('results').hidden = !result;
  if (!result || result === shownResult) return;
  shownResult = result;
  $('parts-body').replaceChildren(...result.parts.map((p, i) => h('tr', {},
    h('td', {}, h('span', { class: 'swatch', style: `background:${partColors[i % partColors.length]}` }), ` ${p.name}`),
    h('td', {}, `${p.size.map((v) => fmt(v, 0)).join(' × ')} mm`),
    h('td', {}, p.face_down ? `Cut ${p.face_down} face` : '–'),
    h('td', {}, String(p.bolts)))));
  $('hardware-body').replaceChildren(...result.hardware.map((x) => h('tr', {},      // "8 | bolts M4 × 16 mm"
    h('td', { class: 'qty' }, String(x.qty)), h('td', {}, `${x.item.toLowerCase()}${x.qty === 1 ? '' : 's'} ${x.spec}`))));
  $('joins').replaceChildren(...result.cuts.map((c) => {
    const joins = c.joins.map((j) => `${j.parts.join(' and ')} (${many(j.bolts, 'bolt')})`).join(', ') || 'no parts';
    const pockets = c.nut_pockets_on ? `, nut pockets on the ${c.nut_pockets_on === 'A' ? 'arrow' : 'other'} side` : '';
    return h('li', {}, h('b', {}, `Cut ${c.cut}: `), `${many(c.bolts, 'bolt')} ${c.bolt_spec}, ${many(c.nuts, 'nut')}, `
      + `${many(c.washers, 'washer')} (${c.fastening.toLowerCase()}${pockets}). Joins ${joins}.`);
  }));
  $('downloads').replaceChildren(
    ...result.parts.map((p) => h('a', { class: 'button', href: p.url, download: p.file }, `Download part ${p.name}`)),
    h('a', { class: 'button primary', href: result.zip, download: '' }, 'Download all (ZIP)'));
}
