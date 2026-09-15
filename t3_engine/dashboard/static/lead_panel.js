/* Lead Engine panel renderer — built once, values updated in place.
 *
 * The defect this replaces: `render()` composed one HTML string for
 * eleven cards and assigned it to `body.innerHTML` every second. Every
 * label, bar and number was destroyed and recreated once a second, which
 * is the jitter you can see, and is also why text shifted horizontally
 * (the digits were proportional-width).
 *
 * There is no React or Vue in this project — the dashboard is a
 * hand-written page — so "use memo/useMemo/selectors" is met by its
 * intent rather than its letter:
 *
 *   BUILD ONCE   `mount()` creates the labels, the cards and the bars a
 *                single time and keeps a reference to every value node.
 *   UPDATE ONLY  `apply()` walks a field map and writes to `textContent`
 *                only where the value actually changed. A field whose
 *                text is identical is not touched, so the browser has no
 *                reason to repaint it.
 *   THROTTLED    the engine recalculates as fast as it likes; the panel
 *                repaints on a fixed cadence (default 300ms) via
 *                requestAnimationFrame, and the newest state REPLACES
 *                any pending one rather than queueing behind it.
 *   DEADBAND     colours only change on a real state change, so a value
 *                oscillating around zero does not strobe green/red.
 *
 * Shared by the tab and the Market Workspace, so both are guaranteed to
 * show the same numbers the same way.
 */
(function (global) {
  'use strict';

  var DEFAULT_REFRESH_MS = 300;

  /* A change smaller than this fraction of the value's own scale is not a
     state change, so the colour does not move. Without it, a net pressure
     drifting across zero repaints green/red several times a second. */
  var DEADBAND = {
    ratio: 0.08,        // signed scores on -1..+1
    pressure: 2.0,      // 0..100
    bps: 0.35,
    generic: 0.0001
  };

  function esc(text) {
    return String(text === undefined || text === null ? '' : text)
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  }

  function fmt(value, digits) {
    if (value === undefined || value === null || value === '') return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return n.toFixed(digits === undefined ? 2 : digits);
  }

  function signed(value, digits) {
    if (value === undefined || value === null) return '—';
    var n = Number(value);
    if (!isFinite(n)) return '—';
    return (n > 0 ? '+' : '') + n.toFixed(digits === undefined ? 2 : digits);
  }

  function ageText(ms) {
    if (ms === undefined || ms === null) return '—';
    var n = Number(ms);
    if (!isFinite(n)) return '—';
    if (n < 1000) return Math.round(n) + 'ms';
    if (n < 90000) return (n / 1000).toFixed(1) + 's';
    return Math.round(n / 60000) + 'm';
  }

  /* ---- the field registry ------------------------------------------- */

  function Panel(root, options) {
    this.root = root;
    this.options = options || {};
    this.refreshMs = this.options.refreshMs || DEFAULT_REFRESH_MS;
    this.nodes = {};        // field id -> element
    this.last = {};         // field id -> last text written
    this.tone = {};         // field id -> last colour class (deadbanded)
    this.toneValue = {};    // field id -> value that produced that class
    this.pending = null;    // newest frame, replaces any older pending one
    this.frameHandle = null;
    this.lastPaint = 0;
    this.paints = 0;
    this.writes = 0;
    this.skipped = 0;
    this.mounted = false;
  }

  /* Register a value cell and return its HTML. The element is looked up
     after the card is inserted, so building is one string per CARD and
     never one per update. */
  Panel.prototype.cell = function (id, className) {
    return '<span class="v le-num ' + (className || '') + '" data-le="' + id + '">—</span>';
  };

  Panel.prototype.row = function (id, label, className) {
    return '<div class="le-row"><span class="k">' + esc(label) + '</span>' +
      this.cell(id, className) + '</div>';
  };

  Panel.prototype.collect = function () {
    var self = this;
    this.nodes = {};
    Array.prototype.forEach.call(this.root.querySelectorAll('[data-le]'), function (el) {
      self.nodes[el.getAttribute('data-le')] = el;
    });
    this.mounted = true;
  };

  /* Write one field, only if it changed. Returns whether it wrote. */
  Panel.prototype.set = function (id, text) {
    var node = this.nodes[id];
    if (!node) return false;
    var value = text === undefined || text === null ? '—' : String(text);
    if (this.last[id] === value) { this.skipped += 1; return false; }
    this.last[id] = value;
    node.textContent = value;
    this.writes += 1;
    return true;
  };

  /* Colour with a deadband: the class only changes when the value has
     moved far enough to be a different state, not merely a different
     number. */
  Panel.prototype.colour = function (id, value, kind) {
    var node = this.nodes[id];
    if (!node) return;
    var band = DEADBAND[kind || 'generic'] || DEADBAND.generic;
    var previous = this.toneValue[id];
    var n = Number(value);
    if (!isFinite(n)) { return; }
    if (previous !== undefined && Math.abs(n - previous) < band) return;
    this.toneValue[id] = n;
    var next = n > band ? 'up' : n < -band ? 'down' : 'dim';
    if (this.tone[id] === next) return;
    node.classList.remove('up', 'down', 'dim');
    node.classList.add(next);
    this.tone[id] = next;
  };

  Panel.prototype.width = function (id, pct) {
    var node = this.nodes[id];
    if (!node) return;
    var value = Math.max(0, Math.min(100, Number(pct) || 0)).toFixed(1) + '%';
    if (this.last['w:' + id] === value) { this.skipped += 1; return; }
    this.last['w:' + id] = value;
    node.style.width = value;
    this.writes += 1;
  };

  Panel.prototype.klass = function (id, className) {
    var node = this.nodes[id];
    if (!node) return;
    if (this.last['c:' + id] === className) return;
    this.last['c:' + id] = className;
    node.className = className;
  };

  /* ---- throttled painting ------------------------------------------- */

  /* The newest frame REPLACES any pending one. A queue of frames would
     mean the panel rendering history at a fixed rate while the market
     moved on; coalescing means it always paints the latest state and
     never falls behind. */
  Panel.prototype.push = function (frame) {
    this.pending = frame;
    this.schedule();
  };

  Panel.prototype.schedule = function () {
    if (this.frameHandle !== null) return;
    var self = this;
    var run = function () {
      self.frameHandle = null;
      var now = (global.performance && performance.now) ? performance.now() : Date.now();
      if (now - self.lastPaint < self.refreshMs) { self.schedule(); return; }
      var frame = self.pending;
      self.pending = null;
      if (!frame) return;
      self.lastPaint = now;
      self.paints += 1;
      try {
        self.paint(frame);
      } catch (e) {
        if (global.console) console.error('lead panel paint failed', e);
      }
    };
    this.frameHandle = (global.requestAnimationFrame
      ? requestAnimationFrame(run) : setTimeout(run, this.refreshMs));
  };

  Panel.prototype.stats = function () {
    return { paints: this.paints, writes: this.writes, skipped: this.skipped,
             refreshMs: this.refreshMs };
  };

  global.LeadPanel = {
    Panel: Panel,
    esc: esc, fmt: fmt, signed: signed, ageText: ageText,
    DEADBAND: DEADBAND, DEFAULT_REFRESH_MS: DEFAULT_REFRESH_MS
  };
})(window);
