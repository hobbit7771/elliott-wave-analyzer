/* Fibonacci retracement as a real drawing tool, mouse and touch.
 *
 * Two taps: the start of the move, then its end. Levels are drawn as
 * price lines on the candle series and labelled with their price.
 *
 * The direction rule, because getting it wrong is silent and is the
 * classic bug: a retracement is measured FROM the end of the move BACK
 * toward its start, so ratio 0 sits at the SECOND point and 1.0 at the
 * FIRST — in both directions. price(r) = end + (start - end) * r needs no
 * conditional, and adding one is what inverts the levels.
 *
 * Touch matters here: on iPhone `click` fires after a 300ms delay and
 * after any scroll gesture, so taps are taken from `touchend` with a
 * movement threshold, and `touchstart` is not consumed — the chart keeps
 * its own pan and pinch.
 *
 * Drawings are kept per symbol AND timeframe. Switching the timeframe
 * swaps which set is on screen; it never mixes them.
 */
(function (global) {
  'use strict';

  var RETRACEMENTS = [0, 0.236, 0.382, 0.5, 0.618, 0.705, 0.786, 1.0];
  var EXTENSIONS = [1.272, 1.414, 1.618, 2.0, 2.618];
  var ALL = RETRACEMENTS.concat(EXTENSIONS);

  var COLORS = {
    0: '#94a3b8', 0.236: '#38bdf8', 0.382: '#34d399', 0.5: '#fbbf24',
    0.618: '#f59e0b', 0.705: '#fb923c', 0.786: '#f87171', 1.0: '#94a3b8'
  };
  var EXTENSION_COLOR = '#a78bfa';

  var STORAGE_PREFIX = 'lead_fib:';
  // A tap that moved more than this many pixels was a drag, not a tap.
  var TAP_SLOP = 12;

  function levels(startPrice, endPrice, ratios) {
    var span = startPrice - endPrice;
    return (ratios || ALL).map(function (ratio) {
      return { ratio: ratio, price: endPrice + span * ratio, extension: ratio > 1.0 };
    });
  }

  function FibTool(options) {
    this.chart = options.chart;
    this.series = options.series;
    this.container = options.container;
    this.onChange = options.onChange || function () {};
    this.symbol = options.symbol;
    this.timeframe = options.timeframe;
    this.ratios = (options.ratios || ALL).slice();
    this.drawings = [];        // { id, start, end, lines: [priceLine], hidden }
    // Nothing has been read from storage yet, so `this.drawings` being
    // empty means "not loaded", not "the user has none". Saving on that
    // distinction is how the saved set got erased - see setChart.
    this.loaded = false;
    this.armed = false;
    this.pendingPoint = null;
    this.hidden = false;
    this._bind();
  }

  FibTool.prototype.key = function () {
    return STORAGE_PREFIX + this.symbol + ':' + this.timeframe;
  };

  /* ---- persistence -------------------------------------------------- */

  FibTool.prototype.save = function () {
    if (!this.loaded) return;    // see setChart: an unloaded tool has
    try {                        // nothing to say about what is stored
      var payload = this.drawings.map(function (d) {
        return { id: d.id, start: d.start, end: d.end, hidden: d.hidden };
      });
      localStorage.setItem(this.key(), JSON.stringify(payload));
    } catch (e) { /* private mode, quota — a lost drawing, not a lost page */ }
  };

  FibTool.prototype.load = function () {
    this.clearLines();
    this.drawings = [];
    this.loaded = true;
    var raw = null;
    try { raw = localStorage.getItem(this.key()); } catch (e) { raw = null; }
    if (!raw) { this.onChange(this); return; }
    var payload;
    try { payload = JSON.parse(raw) || []; } catch (e) { payload = []; }
    var self = this;
    payload.forEach(function (d) {
      if (!d || !d.start || !d.end) return;
      self.drawings.push({ id: d.id, start: d.start, end: d.end,
                           hidden: !!d.hidden, lines: [] });
    });
    this.redraw();
  };

  /* ---- drawing ------------------------------------------------------ */

  FibTool.prototype.arm = function (on) {
    this.armed = on === undefined ? !this.armed : !!on;
    this.pendingPoint = null;
    if (this.container) {
      this.container.style.cursor = this.armed ? 'crosshair' : '';
    }
    return this.armed;
  };

  FibTool.prototype.setRatios = function (ratios) {
    this.ratios = (ratios || []).slice().sort(function (a, b) { return a - b; });
    this.redraw();
  };

  FibTool.prototype.clearLines = function () {
    var self = this;
    this.drawings.forEach(function (d) {
      (d.lines || []).forEach(function (line) {
        try { self.series.removePriceLine(line); } catch (e) {}
      });
      d.lines = [];
    });
  };

  FibTool.prototype.redraw = function () {
    var self = this;
    this.clearLines();
    if (this.hidden) { this.onChange(this); return; }
    this.drawings.forEach(function (d) {
      if (d.hidden) return;
      levels(d.start.price, d.end.price, self.ratios).forEach(function (level) {
        var colour = level.extension ? EXTENSION_COLOR : (COLORS[level.ratio] || '#94a3b8');
        try {
          d.lines.push(self.series.createPriceLine({
            price: level.price,
            color: colour,
            lineWidth: level.ratio === 0 || level.ratio === 1 ? 2 : 1,
            lineStyle: level.extension ? 3 : 2,
            axisLabelVisible: true,
            // The price travels with the label, as the brief asks:
            // "0.618 — 5.9047".
            title: level.ratio.toFixed(3) + ' — ' + formatPrice(level.price)
          }));
        } catch (e) { /* a level off the visible scale is not an error */ }
      });
    });
    this.onChange(this);
  };

  function formatPrice(price) {
    var abs = Math.abs(price);
    var digits = abs >= 1000 ? 2 : abs >= 1 ? 4 : 6;
    return price.toFixed(digits);
  }

  FibTool.prototype.addPoint = function (point) {
    if (!point) return;
    if (!this.pendingPoint) { this.pendingPoint = point; return; }
    var drawing = {
      id: 'fib-' + Date.now(),
      start: this.pendingPoint,
      end: point,
      hidden: false,
      lines: []
    };
    this.pendingPoint = null;
    this.drawings.push(drawing);
    this.arm(false);
    this.save();
    this.redraw();
  };

  FibTool.prototype.deleteLast = function () {
    var gone = this.drawings.pop();
    if (gone) {
      (gone.lines || []).forEach(function (line) {
        try { this.series.removePriceLine(line); } catch (e) {}
      }, this);
    }
    this.save();
    this.redraw();
  };

  FibTool.prototype.toggleHidden = function () {
    this.hidden = !this.hidden;
    this.redraw();
    return this.hidden;
  };

  FibTool.prototype.reset = function () {
    this.clearLines();
    this.drawings = [];
    this.pendingPoint = null;
    this.save();
    this.onChange(this);
  };

  FibTool.prototype.setChart = function (symbol, timeframe) {
    // Switching charts swaps which drawings are on screen. It must never
    // mix them: a level drawn on 5m means nothing on 1h.
    //
    // The save is conditional, and that is the whole bug this fixes. On
    // page open, `start()` calls setChart with the SAME symbol and
    // timeframe the tool was constructed with - so the old unconditional
    // save wrote an empty `drawings` array over the very key it was about
    // to read, and every saved retracement was destroyed by opening the
    // page. Only a tool that has actually loaded something may write, and
    // only when the key is really changing.
    var changing = symbol !== this.symbol || timeframe !== this.timeframe;
    if (this.loaded && changing) this.save();
    this.symbol = symbol;
    this.timeframe = timeframe;
    if (this.loaded && !changing) { this.onChange(this); return; }
    this.load();
  };

  /* ---- input -------------------------------------------------------- */

  FibTool.prototype._point = function (clientX, clientY) {
    var rect = this.container.getBoundingClientRect();
    var x = clientX - rect.left;
    var y = clientY - rect.top;
    var price = this.series.coordinateToPrice(y);
    var time = this.chart.timeScale().coordinateToTime(x);
    if (price === null || price === undefined) return null;
    return { price: Number(price), time: time === null ? undefined : time };
  };

  FibTool.prototype._bind = function () {
    var self = this;
    if (!this.container) return;

    this.container.addEventListener('click', function (event) {
      if (!self.armed || self._fromTouch) return;
      self.addPoint(self._point(event.clientX, event.clientY));
    });

    /* Touch. `click` on iOS arrives late and also fires after a scroll,
       so a tap is recognised from touchend with a movement threshold and
       the click that follows is suppressed. touchstart is deliberately
       NOT consumed, so the chart keeps its own pan and pinch. */
    var startX = 0, startY = 0, startedAt = 0;
    this.container.addEventListener('touchstart', function (event) {
      if (!self.armed || event.touches.length !== 1) return;
      startX = event.touches[0].clientX;
      startY = event.touches[0].clientY;
      startedAt = Date.now();
    }, { passive: true });

    this.container.addEventListener('touchend', function (event) {
      if (!self.armed) return;
      var touch = (event.changedTouches && event.changedTouches[0]);
      if (!touch) return;
      var moved = Math.abs(touch.clientX - startX) + Math.abs(touch.clientY - startY);
      if (moved > TAP_SLOP || Date.now() - startedAt > 800) return;   // a drag or a hold
      event.preventDefault();
      self._fromTouch = true;
      self.addPoint(self._point(touch.clientX, touch.clientY));
      setTimeout(function () { self._fromTouch = false; }, 400);
    }, { passive: false });
  };

  FibTool.prototype.state = function () {
    return {
      armed: this.armed,
      pending: !!this.pendingPoint,
      drawings: this.drawings.length,
      hidden: this.hidden,
      symbol: this.symbol,
      timeframe: this.timeframe
    };
  };

  global.LeadFib = {
    FibTool: FibTool, levels: levels,
    RETRACEMENTS: RETRACEMENTS, EXTENSIONS: EXTENSIONS, ALL: ALL,
    formatPrice: formatPrice
  };
})(window);
