// 2D cut view (SVG) of the selected cut, seen from its arrow side: in-plane x to the right, y up.
// Drawn in screen pixels, so lines and text keep their size whatever the size of the model.
const NS = 'http://www.w3.org/2000/svg';
const PAD = 16;

function add(parent, name, attrs, text) {
  const e = document.createElementNS(NS, name);
  for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
  if (text) e.textContent = text;
  parent.appendChild(e);
  return e;
}

/** The part of segment a-b inside the box [x0, x1] x [y0, y1] (Liang-Barsky), or null. */
function clip([ax, ay], [bx, by], x0, y0, x1, y1) {
  let t0 = 0, t1 = 1;
  const dx = bx - ax, dy = by - ay;
  for (const [p, q] of [[-dx, ax - x0], [dx, x1 - ax], [-dy, ay - y0], [dy, y1 - ay]]) {
    if (p === 0) {
      if (q < 0) return null;
      continue;
    }
    const t = q / p;
    if (p < 0) t0 = Math.max(t0, t);
    else t1 = Math.min(t1, t);
    if (t0 > t1) return null;
  }
  return [[ax + t0 * dx, ay + t0 * dy], [ax + t1 * dx, ay + t1 * dy]];
}

/** A round length for the scale bar: 1, 2 or 5 times a power of ten. */
function niceLength(mm) {
  const p = 10 ** Math.floor(Math.log10(mm));
  return [5, 2, 1].map((k) => k * p).find((v) => v <= mm) || p;
}

const length = (lines) => lines.reduce((s, l) => s + l.slice(1).reduce((t, [x, y], i) => t + Math.hypot(x - l[i][0], y - l[i][1]), 0), 0);
// places for a "mold cavity" label, around its spot, so that no crossing line runs through it
const SPOTS = [[0, 0], [0, 18], [0, -18], [56, 0], [-56, 0], [56, 18], [-56, 18], [56, -18], [-56, -18]];

/** Draw one cut's preview (or nothing). cutColors: color of each cut, for the crossing lines.
 *  Returns which marks that the legend explains only when needed are drawn: {noRoom, blocked}. */
export function drawCutView(svg, cut, cutColors) {
  svg.replaceChildren();
  const W = svg.clientWidth, H = svg.clientHeight;
  if (!cut || W < 50 || H < 50) return {};
  const d = cut.derived, washer = (d.washer || 0) / 2;
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  const grow = (x, y, r = 0) => {
    x0 = Math.min(x0, x - r); y0 = Math.min(y0, y - r);
    x1 = Math.max(x1, x + r); y1 = Math.max(y1, y + r);
  };
  for (const p of [...cut.section, ...cut.flange]) for (const [x, y] of p.exterior) grow(x, y);
  for (const b of cut.bolts) grow(b.xy[0], b.xy[1], washer);
  if (!(x1 > x0) || !(y1 > y0)) return {};
  // room for the labels of crossing lines (right of flat lines, above steep ones) and the scale bar
  const isFlat = (c) => Math.abs(c.line[1][0] - c.line[0][0]) >= Math.abs(c.line[1][1] - c.line[0][1]);
  const right = cut.crossings.some(isFlat) && W > 360 ? 140 : 0, top = cut.crossings.some((c) => !isFlat(c)) ? 22 : 0;
  const w = W - 2 * PAD - right, hgt = H - 2 * PAD - 22 - top;
  const s = Math.min(w / (x1 - x0), hgt / (y1 - y0));
  const ox = PAD + (w - (x1 - x0) * s) / 2, oy = PAD + top + (hgt - (y1 - y0) * s) / 2;
  const X = (x) => ox + (x - x0) * s, Y = (y) => oy + (y1 - y) * s;          // y up
  const pt = ([x, y]) => `${X(x).toFixed(1)},${Y(y).toFixed(1)}`;
  const path = (polys) => polys.map((p) => [p.exterior, ...p.holes].map((r) => `M${r.map(pt).join('L')}Z`).join('')).join('');

  add(svg, 'path', { d: path(cut.flange), class: 'cv-flange', 'fill-rule': 'evenodd' });
  add(svg, 'path', { d: path(cut.section), class: 'cv-section', 'fill-rule': 'evenodd' });
  for (const line of cut.bolt_line) add(svg, 'polyline', { points: line.map(pt).join(' '), class: 'cv-boltline' });
  for (const line of cut.free) add(svg, 'polyline', { points: line.map(pt).join(' '), class: 'cv-free' });
  for (const b of cut.bolts) {
    const g = add(svg, 'g', { class: b.blocked ? 'cv-blocked' : '' });
    const [cx, cy] = [X(b.xy[0]), Y(b.xy[1])];
    add(g, 'circle', { cx, cy, r: Math.max(washer * s, 3), class: 'cv-washer' });
    add(g, 'circle', { cx, cy, r: Math.max((d.hole_diameter / 2) * s, 2), class: 'cv-hole' });
  }
  const drawn = [];                  // the crossing lines as drawn, in pixels
  for (const c of cut.crossings) {   // another cut crosses this one along a line: draw it across the view
    const [a, b] = c.line.map(([x, y]) => [X(x), Y(y)]);
    const len = Math.hypot(b[0] - a[0], b[1] - a[1]) || 1, u = [(b[0] - a[0]) / len, (b[1] - a[1]) / len];
    const seg = clip([a[0] - 1e4 * u[0], a[1] - 1e4 * u[1]], [b[0] + 1e4 * u[0], b[1] + 1e4 * u[1]], 2, 2, W - 2, H - 26);
    if (!seg) continue;
    drawn.push(seg);
    add(svg, 'line', { x1: seg[0][0], y1: seg[0][1], x2: seg[1][0], y2: seg[1][1], class: 'cv-cross',
      stroke: cutColors[c.cut] || '#333' });
    // the label goes in the room kept free: above the right end of a flat line, beside the top of a steep one
    const flat = isFlat(c);
    const [ex, ey] = flat ? (seg[0][0] > seg[1][0] ? seg[0] : seg[1]) : (seg[0][1] < seg[1][1] ? seg[0] : seg[1]);
    const left = !flat && ex > W - 150;
    add(svg, 'text', { x: flat ? ex - 4 : ex + (left ? -8 : 8), y: flat ? Math.max(ey - 8, 14) : ey + 16,
      'text-anchor': flat || left ? 'end' : 'start', class: 'cv-text' }, `Cut ${c.cut + 1} crosses here`);
  }
  const clear = (x, y) => !drawn.some(([a, b]) => clip(a, b, x - 46, y - 13, x + 46, y + 5));   // the text's box
  for (const [x, y] of cut.cavities) {
    const [dx, dy] = SPOTS.find(([u, v]) => clear(X(x) + u, Y(y) + 5 + v)) || [0, 0];
    add(svg, 'text', { x: X(x) + dx, y: Y(y) + 5 + dy, class: 'cv-text cv-cavity', 'text-anchor': 'middle' }, 'mold cavity');
  }
  // scale bar, bottom left
  const mm = niceLength((W * 0.22) / s), y = H - 8;
  add(svg, 'path', { d: `M${PAD},${y - 6}V${y}H${PAD + mm * s}V${y - 6}`, class: 'cv-scale', fill: 'none' });
  add(svg, 'text', { x: PAD + mm * s + 8, y: y + 1, class: 'cv-text' }, `${mm} mm`);
  return { blocked: cut.bolts.some((b) => b.blocked), noRoom: length(cut.free) < length(cut.bolt_line) - 0.5 };
}
