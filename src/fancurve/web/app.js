'use strict';

const SVGNS = 'http://www.w3.org/2000/svg';
const SENSOR_COLORS = { cpu: '--s-cpu', system: '--s-system', nvme: '--s-nvme', gpu: '--s-gpu' };
const FALLBACK_SENSOR_COLORS = ['--s-cpu', '--s-system', '--s-nvme', '--s-gpu'];
const FAN_COLORS = ['--f-0', '--f-1', '--f-2'];
const WINDOW_S = 15 * 60;
const T_MIN = 20, T_MAX = 100;
const FAULT_LABELS = { '': 'healthy', missing: 'unplugged', garbage: 'garbage', frozen: 'frozen', disconnected: 'open circuit' };

const state = {
  config: null,
  status: null,
  history: [],
  lastT: 0,
  drafts: {},
  token: new URLSearchParams(location.search).get('token') || '',
};

function css(v) { return getComputedStyle(document.documentElement).getPropertyValue(v).trim(); }

function h(tag, attrs, ...kids) {
  const el = tag.startsWith('svg:') ? document.createElementNS(SVGNS, tag.slice(4)) : document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === 'style') for (const [p, pv] of Object.entries(v)) el.style.setProperty(p, pv);
    else if (k === 'text') el.textContent = v;
    else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
    else el.setAttribute(k, v === true ? '' : v);
  }
  for (const kid of kids.flat()) if (kid != null) el.append(kid);
  return el;
}

async function api(path, opts = {}) {
  const headers = Object.assign({}, opts.headers || {});
  if (state.token) headers.Authorization = 'Bearer ' + state.token;
  if (opts.body !== undefined) headers['Content-Type'] = 'application/json';
  const res = await fetch(path, {
    method: opts.method || 'GET',
    headers,
    body: opts.body === undefined ? undefined : JSON.stringify(opts.body),
  });
  let data = null;
  try { data = await res.json(); } catch (e) { data = { error: 'invalid response' }; }
  if (!res.ok) { const err = new Error(data.error || res.statusText); err.data = data; throw err; }
  return data;
}

function sensorColor(id) {
  if (SENSOR_COLORS[id]) return css(SENSOR_COLORS[id]);
  const ids = Object.keys(state.config.sensors);
  return css(FALLBACK_SENSOR_COLORS[ids.indexOf(id) % FALLBACK_SENSOR_COLORS.length]);
}
function fanColor(id) {
  const ids = Object.keys(state.config.fans);
  return css(FAN_COLORS[ids.indexOf(id) % FAN_COLORS.length]);
}
function heat(t) {
  if (t == null) return css('--muted');
  if (t < 55) return css('--ok');
  if (t < 70) return css('--warn');
  if (t < 82) return css('--hot');
  return css('--crit');
}
const fmt1 = v => (v == null ? '--' : (Math.round(v * 10) / 10).toFixed(1));
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));
const clock = t => new Date(t * 1000).toLocaleTimeString([], { hour12: false });
const sensorLabel = id => (state.config.sensors[id] || {}).label || id;

function buildSensors() {
  const box = document.getElementById('sensors');
  box.replaceChildren();
  for (const [id, s] of Object.entries(state.config.sensors)) {
    box.append(h('div', { class: 'card', id: 'sc-' + id },
      h('div', { class: 'tile-h' },
        h('span', { class: 'dot', style: { '--c': sensorColor(id) } }), s.label,
        h('span', { class: 'tag', id: 'ss-' + id, text: '-' })),
      h('div', {}, h('span', { class: 'val', id: 'sv-' + id, text: '--' }), h('span', { class: 'u', text: '°C' })),
      h('div', { class: 'meter' }, h('i', { id: 'sm-' + id })),
      h('div', { class: 'meta', id: 'sd-' + id })));
  }
}

function buildFans() {
  const box = document.getElementById('fans');
  box.replaceChildren();
  for (const [id, f] of Object.entries(state.config.fans)) {
    box.append(h('div', { class: 'card', id: 'fc-' + id },
      h('div', { class: 'tile-h' },
        h('span', { class: 'dot', style: { '--c': fanColor(id) } }), f.label,
        h('span', { class: 'sub', text: 'pwm' + f.channel }),
        h('span', { class: 'tag', id: 'fm-' + id, text: '-' })),
      h('div', { class: 'split' },
        h('div', {}, h('span', { class: 'val', id: 'fr-' + id, text: '--' }), h('span', { class: 'u', text: 'RPM' })),
        h('span', { class: 'duty', id: 'fp-' + id, text: '--' })),
      h('div', { class: 'meter' }, h('i', { id: 'fb-' + id, style: { '--c': fanColor(id) } })),
      h('div', { class: 'meta', id: 'fd-' + id })));
  }
}

function buildLegends() {
  const lt = document.getElementById('leg-temp');
  lt.replaceChildren(...Object.entries(state.config.sensors).map(([id, s]) =>
    h('span', { style: { '--c': sensorColor(id) } }, h('i'), s.label.split(' (')[0])));
  const ld = document.getElementById('leg-duty');
  ld.replaceChildren(...Object.entries(state.config.fans).map(([id, f]) =>
    h('span', { style: { '--c': fanColor(id) } }, h('i'), f.label)),
    h('span', { style: { '--c': css('--crit') } }, h('i'), 'fail-safe'));
}

function lineChart(svg, series, opt) {
  const W = 600, H = 220, L = 34, R = 8, T = 8, B = 20;
  const now = Date.now() / 1000, t0 = now - WINDOW_S;
  const X = t => L + (W - L - R) * (t - t0) / WINDOW_S;
  const Y = v => (H - B) - (H - T - B) * (clamp(v, opt.min, opt.max) - opt.min) / (opt.max - opt.min);
  const kids = [];
  for (let v = opt.min; v <= opt.max + 1e-9; v += opt.step) {
    kids.push(h('svg:line', { class: 'gridline', x1: L, x2: W - R, y1: Y(v), y2: Y(v) }));
    kids.push(h('svg:text', { x: L - 6, y: Y(v) + 3.5, 'text-anchor': 'end', text: v + (opt.unit || '') }));
  }
  for (let m = 15; m >= 0; m -= 5) {
    const x = X(now - m * 60);
    kids.push(h('svg:text', { x, y: H - 4, 'text-anchor': m === 0 ? 'end' : 'middle', text: m === 0 ? 'now' : '-' + m + 'm' }));
  }
  (opt.bands || []).forEach(([a, b]) => {
    kids.push(h('svg:rect', { class: 'fs-band', x: X(Math.max(a, t0)), y: T, width: Math.max(2, X(b) - X(Math.max(a, t0))), height: H - T - B }));
  });
  for (const s of series) {
    let d = '', pen = false;
    for (const [t, v] of s.points) {
      if (t < t0) continue;
      if (v == null) { pen = false; continue; }
      d += (pen ? 'L' : 'M') + X(t).toFixed(1) + ' ' + Y(v).toFixed(1);
      pen = true;
    }
    if (d) kids.push(h('svg:path', { class: 'series', d, stroke: s.color }));
  }
  if (!state.history.length) kids.push(h('svg:text', { class: 'empty-note', x: W / 2, y: H / 2, 'text-anchor': 'middle', text: 'collecting samples' }));
  svg.replaceChildren(...kids);
}

function failsafeBands() {
  const bands = [];
  let start = null;
  const hist = state.history;
  hist.forEach((s, i) => {
    const on = s.failsafe && s.failsafe.length > 0;
    if (on && start == null) start = s.t;
    if ((!on || i === hist.length - 1) && start != null) { bands.push([start, s.t + 1]); start = null; }
  });
  return bands;
}

function drawCharts() {
  const hist = state.history;
  lineChart(document.getElementById('chart-temp'),
    Object.keys(state.config.sensors).map(id => ({ color: sensorColor(id), points: hist.map(s => [s.t, s.temps[id]]) })),
    { min: 20, max: 90, step: 10, unit: '°' });
  lineChart(document.getElementById('chart-duty'),
    Object.keys(state.config.fans).map(id => ({ color: fanColor(id), points: hist.map(s => [s.t, s.pwm[id]]) })),
    { min: 0, max: 100, step: 20, unit: '%', bands: failsafeBands() });
}

const ED = { W: 400, H: 230, L: 32, R: 8, T: 8, B: 22 };
const EX = t => ED.L + (ED.W - ED.L - ED.R) * (t - T_MIN) / (T_MAX - T_MIN);
const EY = p => (ED.H - ED.B) - (ED.H - ED.T - ED.B) * p / 100;

function draftFor(id) {
  if (!state.drafts[id]) {
    const f = state.config.fans[id];
    state.drafts[id] = {
      curve: f.curve.map(p => p.slice()), source: f.source, min_pwm: f.min_pwm,
      hysteresis_c: f.hysteresis_c, dirty: false,
    };
  }
  return state.drafts[id];
}

function resetDraft(id) { delete state.drafts[id]; draftFor(id); syncEditor(id); }

function buildEditors() {
  const box = document.getElementById('editors');
  box.replaceChildren();
  const presets = Object.keys(state.config.presets);
  for (const [id, f] of Object.entries(state.config.fans)) {
    draftFor(id);
    const sourceSel = h('select', { id: 'src-' + id, onchange: e => { draftFor(id).source = e.target.value; touch(id); } },
      Object.entries(state.config.sensors).map(([sid, s]) => h('option', { value: sid, text: s.label })));
    const presetSel = h('select', { id: 'pre-' + id, onchange: e => applyPreset(id, e.target.value) },
      h('option', { value: '', text: 'custom' }),
      presets.map(p => h('option', { value: p, text: p[0].toUpperCase() + p.slice(1) })));
    const floor = h('input', { type: 'number', min: 0, max: 100, step: 1, id: 'min-' + id,
      onchange: e => { draftFor(id).min_pwm = Number(e.target.value); touch(id); } });
    const hyst = h('input', { type: 'number', min: 0, max: 15, step: 0.5, id: 'hy-' + id,
      onchange: e => { draftFor(id).hysteresis_c = Number(e.target.value); touch(id); } });
    const svg = h('svg:svg', { class: 'editor', id: 'ed-' + id, viewBox: `0 0 ${ED.W} ${ED.H}`, role: 'application', 'aria-label': 'Fan curve for ' + f.label });
    box.append(h('div', { class: 'card', id: 'ec-' + id },
      h('div', { class: 'ed-head' },
        h('h3', {}, h('span', { class: 'dot', style: { '--c': fanColor(id) } }), f.label),
        h('span', { class: 'tag', id: 'em-' + id, text: '-' })),
      h('div', { class: 'controls' },
        h('div', { class: 'field' }, h('label', { for: 'src-' + id, text: 'Temperature source' }), sourceSel),
        h('div', { class: 'field' }, h('label', { for: 'pre-' + id, text: 'Preset' }), presetSel),
        h('div', { class: 'field' }, h('label', { for: 'min-' + id, text: 'Floor (min duty %)' }), floor),
        h('div', { class: 'field' }, h('label', { for: 'hy-' + id, text: 'Hysteresis (°C)' }), hyst)),
      svg,
      h('div', { class: 'live', id: 'el-' + id }),
      h('div', { class: 'pts', id: 'ep-' + id }),
      h('ul', { class: 'errors', id: 'ee-' + id, hidden: true }),
      h('div', { class: 'ed-foot' },
        h('button', { class: 'btn ghost', id: 'rv-' + id, onclick: () => { resetDraft(id); flash(id, 'reverted'); } }, 'Revert'),
        h('button', { class: 'btn', id: 'sv-btn-' + id, disabled: true, onclick: () => save(id) }, 'Save and apply'),
        h('span', { class: 'msg', id: 'msg-' + id }))));
    wireDrag(id, svg);
    syncEditor(id);
  }
}

function matchPreset(curve) {
  const s = JSON.stringify(curve);
  for (const [k, v] of Object.entries(state.config.presets)) if (JSON.stringify(v) === s) return k;
  return '';
}

function applyPreset(id, name) {
  if (!name) return;
  const d = draftFor(id);
  d.curve = state.config.presets[name].map(p => p.slice());
  touch(id);
}

function touch(id) { draftFor(id).dirty = true; syncEditor(id); }

function syncEditor(id) {
  const d = draftFor(id);
  document.getElementById('src-' + id).value = d.source;
  document.getElementById('pre-' + id).value = matchPreset(d.curve);
  document.getElementById('min-' + id).value = d.min_pwm;
  document.getElementById('hy-' + id).value = d.hysteresis_c;
  document.getElementById('sv-btn-' + id).disabled = !d.dirty;
  renderPoints(id);
  drawEditor(id);
}

function renderPoints(id) {
  const d = draftFor(id);
  const box = document.getElementById('ep-' + id);
  if (box.contains(document.activeElement)) return;
  const lastK = d.curve.length - 1;
  box.style.setProperty('--n', d.curve.length);
  const temps = d.curve.map((p, k) => h('input', {
    type: 'number', value: p[0], step: 1, 'aria-label': `point ${k + 1} temperature`,
    onchange: e => setPoint(id, k, Number(e.target.value), d.curve[k][1]),
  }));
  const duties = d.curve.map((p, k) => h('input', {
    type: 'number', value: p[1], step: 1, disabled: k === lastK,
    title: k === lastK ? 'The last point is always 100 percent' : null,
    'aria-label': `point ${k + 1} duty`,
    onchange: e => setPoint(id, k, d.curve[k][0], Number(e.target.value)),
  }));
  box.replaceChildren(h('span', { text: '°C' }), ...temps, h('span', { text: 'duty' }), ...duties);
}

function setPoint(id, k, t, p) {
  const c = draftFor(id).curve;
  const lastK = c.length - 1;
  const loT = k > 0 ? c[k - 1][0] + 1 : T_MIN;
  const hiT = k < lastK ? c[k + 1][0] - 1 : T_MAX;
  const loP = k > 0 ? c[k - 1][1] : 0;
  const hiP = k < lastK ? c[k + 1][1] : 100;
  c[k][0] = clamp(Math.round(t), loT, hiT);
  c[k][1] = k === lastK ? 100 : clamp(Math.round(p), loP, hiP);
  touch(id);
}

function drawEditor(id) {
  const svg = document.getElementById('ed-' + id);
  if (!svg) return;
  const d = draftFor(id);
  const col = fanColor(id);
  const pts = d.curve;
  const kids = [];
  kids.push(h('svg:defs', {},
    h('svg:pattern', { id: 'hatch-' + id, width: 6, height: 6, patternUnits: 'userSpaceOnUse', patternTransform: 'rotate(45)' },
      h('svg:line', { x1: 0, y1: 0, x2: 0, y2: 6, stroke: css('--axis'), 'stroke-width': 2 })),
    h('svg:linearGradient', { id: 'grad-' + id, x1: 0, y1: 0, x2: 0, y2: 1 },
      h('svg:stop', { offset: '0', 'stop-color': col, 'stop-opacity': 0.32 }),
      h('svg:stop', { offset: '1', 'stop-color': col, 'stop-opacity': 0.03 }))));
  for (let p = 0; p <= 100; p += 20) {
    kids.push(h('svg:line', { class: 'gridline', x1: ED.L, x2: ED.W - ED.R, y1: EY(p), y2: EY(p) }));
    kids.push(h('svg:text', { x: ED.L - 5, y: EY(p) + 3.5, 'text-anchor': 'end', text: p + '%' }));
  }
  for (let t = T_MIN; t <= T_MAX; t += 10) {
    kids.push(h('svg:line', { class: 'gridline', x1: EX(t), x2: EX(t), y1: ED.T, y2: ED.H - ED.B }));
    kids.push(h('svg:text', { x: EX(t), y: ED.H - 7, 'text-anchor': 'middle', text: t + '°' }));
  }
  kids.push(h('svg:rect', { class: 'floor', fill: `url(#hatch-${id})`, x: ED.L, y: EY(d.min_pwm), width: ED.W - ED.L - ED.R, height: EY(0) - EY(d.min_pwm) }));
  kids.push(h('svg:line', { class: 'floor-line', x1: ED.L, x2: ED.W - ED.R, y1: EY(d.min_pwm), y2: EY(d.min_pwm) }));
  kids.push(h('svg:text', { x: ED.W - ED.R - 4, y: EY(d.min_pwm) - 5, 'text-anchor': 'end', text: 'floor ' + d.min_pwm + '%' }));

  const eff = p => Math.max(p, d.min_pwm);
  const line = [[T_MIN, eff(pts[0][1])], ...pts.map(p => [p[0], eff(p[1])]), [T_MAX, 100]];
  const path = line.map((p, i) => (i ? 'L' : 'M') + EX(p[0]).toFixed(1) + ' ' + EY(p[1]).toFixed(1)).join('');
  kids.push(h('svg:path', { class: 'area', d: path + `L${EX(T_MAX)} ${EY(0)}L${EX(T_MIN)} ${EY(0)}Z`, fill: `url(#grad-${id})` }));
  kids.push(h('svg:path', { class: 'curve', d: path, stroke: col }));

  const st = state.status && state.status.fans[id];
  if (st && st.temp != null && d.source === st.source) {
    const x = EX(clamp(st.temp, T_MIN, T_MAX));
    kids.push(h('svg:line', { class: 'now-line', x1: x, x2: x, y1: ED.T, y2: ED.H - ED.B }));
    if (st.effective_temp != null && st.effective_temp > st.temp + 0.05) {
      const y = EY(st.target_pct);
      kids.push(h('svg:line', { class: 'hyst', x1: x, x2: EX(clamp(st.effective_temp, T_MIN, T_MAX)), y1: y, y2: y }));
    }
    if (st.target_pct != null) {
      kids.push(h('svg:circle', { class: 'now-dot', cx: EX(clamp(st.effective_temp ?? st.temp, T_MIN, T_MAX)), cy: EY(st.target_pct), r: 4.5 }));
    }
  }
  pts.forEach((p, k) => {
    const last = k === pts.length - 1;
    kids.push(h('svg:circle', {
      class: 'handle' + (last ? ' locked' : ''), cx: EX(p[0]), cy: EY(p[1]), r: 7.5, fill: col,
      tabindex: 0, 'data-k': k, role: 'slider',
      'aria-label': `point ${k + 1}: ${p[0]} degrees, ${p[1]} percent`,
    }));
  });
  const focused = svg.contains(document.activeElement) ? document.activeElement.getAttribute('data-k') : null;
  svg.replaceChildren(...kids);
  if (focused != null) { const el = svg.querySelector(`[data-k="${focused}"]`); if (el) el.focus(); }
}

function wireDrag(id, svg) {
  let drag = null;
  const toData = ev => {
    const pt = svg.createSVGPoint();
    pt.x = ev.clientX; pt.y = ev.clientY;
    const q = pt.matrixTransform(svg.getScreenCTM().inverse());
    return [T_MIN + (T_MAX - T_MIN) * (q.x - ED.L) / (ED.W - ED.L - ED.R), 100 * ((ED.H - ED.B) - q.y) / (ED.H - ED.T - ED.B)];
  };
  svg.addEventListener('pointerdown', ev => {
    const [t, p] = toData(ev);
    const c = draftFor(id).curve;
    let best = 0, bd = Infinity;
    c.forEach((pt, k) => { const dd = (EX(pt[0]) - EX(t)) ** 2 + (EY(pt[1]) - EY(p)) ** 2; if (dd < bd) { bd = dd; best = k; } });
    if (bd > 40 * 40) return;
    drag = best;
    svg.setPointerCapture(ev.pointerId);
    ev.preventDefault();
  });
  svg.addEventListener('pointermove', ev => {
    if (drag == null) return;
    const [t, p] = toData(ev);
    setPoint(id, drag, t, p);
  });
  const end = () => { drag = null; };
  svg.addEventListener('pointerup', end);
  svg.addEventListener('pointercancel', end);
  svg.addEventListener('keydown', ev => {
    const k = ev.target.getAttribute && ev.target.getAttribute('data-k');
    if (k == null) return;
    const c = draftFor(id).curve[+k];
    const step = ev.shiftKey ? 5 : 1;
    const moves = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, step], ArrowDown: [0, -step] };
    const m = moves[ev.key];
    if (!m) return;
    ev.preventDefault();
    setPoint(id, +k, c[0] + m[0], c[1] + m[1]);
    const again = svg.querySelector(`[data-k="${k}"]`);
    if (again) again.focus();
  });
}

function flash(id, text, kind) {
  const m = document.getElementById('msg-' + id);
  m.textContent = text;
  m.className = 'msg' + (kind ? ' ' + kind : '');
  clearTimeout(m._t);
  m._t = setTimeout(() => { m.textContent = ''; m.className = 'msg'; }, 3500);
}

async function save(id) {
  const d = draftFor(id);
  const f = state.config.fans[id];
  const body = { curve: d.curve };
  if (d.source !== f.source) body.source = d.source;
  if (d.min_pwm !== f.min_pwm) body.min_pwm = d.min_pwm;
  if (d.hysteresis_c !== f.hysteresis_c) body.hysteresis_c = d.hysteresis_c;
  const errs = document.getElementById('ee-' + id);
  errs.hidden = true;
  flash(id, 'saving');
  try {
    const r = await api('api/fans/' + encodeURIComponent(id), { method: 'PUT', body });
    state.config.fans[id] = r.fan;
    resetDraft(id);
    flash(id, 'applied', 'ok');
  } catch (e) {
    flash(id, e.message, 'err');
    const list = (e.data && e.data.errors) || [];
    errs.replaceChildren(...list.map(x => h('li', { text: x })));
    errs.hidden = list.length === 0;
  }
}

function renderStatus() {
  const s = state.status;
  const st = document.getElementById('state');
  st.dataset.state = s.state;
  st.textContent = { controlling: 'controlling', degraded: 'degraded', failsafe: 'fail-safe', waiting: 'waiting for chip', released: 'released' }[s.state] || s.state;
  document.getElementById('chip').textContent = s.chip;
  document.getElementById('updated').textContent = `${clock(s.time)} · tick ${s.loop.ticks} · every ${s.loop.interval_s}s`;

  const failing = Object.entries(s.fans).filter(([, f]) => f.mode === 'failsafe');
  const banner = document.getElementById('banner');
  if (failing.length) {
    banner.className = 'banner';
    banner.replaceChildren(h('strong', { text: `${failing.length} fan${failing.length > 1 ? 's' : ''} forced to 100 percent.` }),
      ' The controller does not trust its input and chose noise over heat.',
      h('ul', {}, failing.map(([id, f]) => h('li', { text: `${state.config.fans[id].label}: ${f.reason}` }))));
    banner.hidden = false;
  } else if (s.state === 'degraded') {
    banner.className = 'banner warn';
    const holding = Object.entries(s.sensors).filter(([, x]) => x.status === 'holding');
    banner.replaceChildren(h('strong', { text: 'Bridging a sensor gap.' }),
      ' Using the last good reading until it recovers or the grace period runs out.',
      h('ul', {}, holding.map(([id, x]) => h('li', { text: `${sensorLabel(id)}: ${x.detail}` }))));
    banner.hidden = false;
  } else {
    banner.hidden = true;
  }

  for (const [id, x] of Object.entries(s.sensors)) {
    const good = x.status === 'ok' || x.status === 'holding';
    document.getElementById('sv-' + id).textContent = good ? fmt1(x.value) : '--';
    document.getElementById('sv-' + id).style.color = good ? heat(x.value) : css('--muted');
    const tag = document.getElementById('ss-' + id);
    tag.textContent = x.status.replace('_', ' ');
    tag.className = 'tag ' + (x.status === 'ok' ? 'ok' : x.status === 'holding' ? 'holding' : 'bad');
    const m = document.getElementById('sm-' + id);
    m.style.width = good ? clamp((x.value - 20) / 70 * 100, 0, 100) + '%' : '0';
    m.style.background = heat(x.value);
    const users = Object.entries(s.fans).filter(([, f]) => f.source === id).map(([fid]) => state.config.fans[fid].label);
    document.getElementById('sd-' + id).textContent = x.detail || (users.length ? 'drives ' + users.join(', ') : 'not driving a fan');
    document.getElementById('sc-' + id).className = 'card' + (good ? (x.status === 'holding' ? ' meh' : '') : ' bad');
  }

  for (const [id, f] of Object.entries(s.fans)) {
    document.getElementById('fr-' + id).textContent = f.rpm == null ? '--' : f.rpm;
    document.getElementById('fp-' + id).textContent = f.target_pct == null ? '--' : Math.round(f.target_pct) + '%';
    document.getElementById('fb-' + id).style.width = (f.target_pct || 0) + '%';
    const tag = document.getElementById('fm-' + id);
    tag.textContent = f.mode === 'failsafe' ? 'fail-safe' : f.mode;
    tag.className = 'tag ' + (f.mode === 'failsafe' ? 'failsafe' : f.mode === 'curve' ? 'ok' : '');
    const em = document.getElementById('em-' + id);
    em.textContent = tag.textContent; em.className = tag.className;
    const meta = document.getElementById('fd-' + id);
    if (f.mode === 'failsafe') meta.textContent = f.reason;
    else if (f.temp != null) {
      meta.replaceChildren('from ', h('b', { text: sensorLabel(f.source) }), ` ${fmt1(f.temp)} °C`,
        f.effective_temp > f.temp + 0.05 ? ` (curve at ${fmt1(f.effective_temp)} °C)` : '',
        ` · floor ${f.min_pwm}%`);
    }
    document.getElementById('fc-' + id).className = 'card' + (f.mode === 'failsafe' ? ' bad' : '');
    const live = document.getElementById('el-' + id);
    if (live) {
      live.replaceChildren(f.mode === 'failsafe'
        ? h('span', { text: 'Fail-safe: curve ignored, running at 100 percent.' })
        : h('span', {}, 'Now ', h('b', { text: fmt1(f.temp) + ' °C' }),
          f.effective_temp > f.temp + 0.05 ? ` (curve reads ${fmt1(f.effective_temp)} °C, hysteresis)` : '',
          ' → ', h('b', { text: Math.round(f.target_pct || 0) + '%' }), ` · ${f.rpm ?? '--'} RPM`));
    }
    drawEditor(id);
  }

  const ev = document.getElementById('events');
  ev.replaceChildren(...s.events.slice().reverse().map(e =>
    h('li', {}, h('time', { text: clock(e.t) }), h('span', { class: 'lvl ' + e.level, text: e.level }), h('span', { text: e.message }))));

  renderDemo(s.demo);
}

function renderDemo(d) {
  const box = document.getElementById('demo');
  if (!d) { box.hidden = true; return; }
  box.hidden = false;
  const p = d.power_w || {};
  document.getElementById('demo-phase').textContent =
    `phase ${d.phase} · CPU ${Math.round(p.cpu || 0)} W · GPU ${Math.round(p.gpu || 0)} W · NVMe ${(p.nvme || 0).toFixed(1)} W`;
  document.querySelectorAll('#workloads button').forEach(b => b.classList.toggle('on', b.dataset.w === d.workload));
  for (const id of Object.keys(state.config.sensors)) {
    const sel = document.getElementById('fault-' + id);
    if (sel && document.activeElement !== sel) {
      sel.value = d.faults[id] || '';
      sel.classList.toggle('active', !!d.faults[id]);
    }
  }
}

function buildDemo() {
  const box = document.getElementById('faults');
  box.replaceChildren(...Object.entries(state.config.sensors).map(([id, s]) =>
    h('div', { class: 'field' }, h('label', { for: 'fault-' + id, text: s.label.split(' (')[0] }),
      h('select', { id: 'fault-' + id, onchange: e => demo('fault', { sensor: id, fault: e.target.value || null }) },
        Object.entries(FAULT_LABELS).map(([v, t]) => h('option', { value: v, text: t }))))));
  document.getElementById('workloads').addEventListener('click', e => {
    const w = e.target.dataset && e.target.dataset.w;
    if (w) demo('workload', { workload: w });
  });
  document.getElementById('crash').addEventListener('click', () => demo('crash', {}));
  document.getElementById('clear-faults').addEventListener('click', async () => {
    for (const id of Object.keys(state.config.sensors)) await demo('fault', { sensor: id, fault: null });
  });
}

async function demo(action, body) {
  try {
    const r = await api('api/demo/' + action, { method: 'POST', body });
    renderDemo(r.demo);
    poll();
  } catch (e) { console.error(e); }
}

let polling = false;
async function poll() {
  if (polling) return;
  polling = true;
  try {
    const [status, hist] = await Promise.all([api('api/status'), api('api/history?since=' + state.lastT)]);
    state.status = status;
    if (hist.samples.length) {
      state.history.push(...hist.samples);
      state.lastT = hist.samples[hist.samples.length - 1].t;
      const cut = Date.now() / 1000 - WINDOW_S - 5;
      while (state.history.length && state.history[0].t < cut) state.history.shift();
    }
    renderStatus();
    drawCharts();
  } catch (e) {
    const st = document.getElementById('state');
    st.dataset.state = 'waiting';
    st.textContent = e.message.includes('token') ? 'token required' : 'offline';
  } finally {
    polling = false;
  }
}

async function boot() {
  try {
    state.config = await api('api/config');
  } catch (e) {
    document.getElementById('state').textContent = e.message.includes('token') ? 'token required' : 'offline';
    setTimeout(boot, 3000);
    return;
  }
  buildSensors();
  buildFans();
  buildLegends();
  buildEditors();
  if (state.config.demo) buildDemo();
  await poll();
  setInterval(poll, 1000);
  window.addEventListener('resize', drawCharts);
}

boot();
