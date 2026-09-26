/* Общие утилиты: время (МСК), форматирование, уровни риска, поиск, геометрия сети. */
(function () {
  'use strict';

  const TZ = 'Europe/Moscow';
  const fmtHM = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit' });
  const fmtHMS = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, hour: '2-digit', minute: '2-digit', second: '2-digit' });
  const fmtDate = new Intl.DateTimeFormat('ru-RU', { timeZone: TZ, weekday: 'short', day: 'numeric', month: 'long', year: 'numeric' });

  const SEV = {
    critical: { label: 'Опоздание', short: 'Опоздание', color: 'var(--crit)', rank: 3 },
    warning: { label: 'Риск опоздания', short: 'Риск', color: 'var(--warn)', rank: 2 },
    early: { label: 'Опережение', short: 'Опережение', color: 'var(--early)', rank: 1 },
    ok: { label: 'В графике', short: 'В графике', color: 'var(--ok)', rank: 0 },
    unknown: { label: 'Нет данных', short: 'Нет данных', color: 'var(--unknown)', rank: -1 },
  };

  // Форма значка дублирует цвет: круг «!» — опоздание, треугольник — риск,
  // ромб — опережение, круг с галочкой — в графике, пунктирный круг — нет данных.
  function sevIcon(sev, size) {
    const s = size || 14;
    const c = (SEV[sev] || SEV.unknown).color;
    const svg = (inner) => `<svg width="${s}" height="${s}" viewBox="0 0 16 16" aria-hidden="true">${inner}</svg>`;
    switch (sev) {
      case 'critical':
        return svg(`<circle cx="8" cy="8" r="7" fill="${c}"/><rect x="7" y="3.5" width="2" height="6" rx="1" fill="#fff"/><circle cx="8" cy="12" r="1.1" fill="#fff"/>`);
      case 'warning':
        return svg(`<path d="M8 1.2 15.2 14H.8z" fill="${c}"/><rect x="7.1" y="5.5" width="1.8" height="4.6" rx=".9" fill="#1a1a19"/><circle cx="8" cy="12" r="1" fill="#1a1a19"/>`);
      case 'early':
        return svg(`<path d="M8 .8 15.2 8 8 15.2.8 8z" fill="${c}"/><path d="M9.8 5 6.8 8l3 3" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`);
      case 'ok':
        return svg(`<circle cx="8" cy="8" r="7" fill="${c}"/><path d="m4.8 8.2 2.2 2.2 4.2-4.6" stroke="#fff" stroke-width="1.8" fill="none" stroke-linecap="round" stroke-linejoin="round"/>`);
      default:
        return svg(`<circle cx="8" cy="8" r="6.2" fill="none" stroke="${c}" stroke-width="1.8" stroke-dasharray="2.6 2"/>`);
    }
  }

  function sevBadge(sev, text) {
    const d = SEV[sev] || SEV.unknown;
    return `<span class="sev">${sevIcon(sev)}${text || d.label}</span>`;
  }

  function sign(x) { return x > 0 ? '+' : x < 0 ? '−' : ''; }

  const U = {
    TZ, SEV, sevIcon, sevBadge,

    time(ep, withSec) {
      if (ep == null || isNaN(ep)) return '—';
      return (withSec ? fmtHMS : fmtHM).format(new Date(ep * 1000));
    },
    date(ep) { return fmtDate.format(new Date(ep * 1000)); },

    /** Отклонение в виде «+4:10» / «−0:45». */
    delay(s) {
      if (s == null || isNaN(s)) return '—';
      const a = Math.round(Math.abs(s));
      return `${sign(Math.round(s))}${Math.floor(a / 60)}:${String(a % 60).padStart(2, '0')}`;
    },
    /** Длительность «12 мин» / «45 с». */
    dur(s) {
      if (s == null || isNaN(s)) return '—';
      s = Math.round(Math.abs(s));
      if (s < 60) return `${s} с`;
      const m = Math.floor(s / 60);
      if (m < 60) return `${m} мин`;
      return `${Math.floor(m / 60)} ч ${m % 60} мин`;
    },
    num(x, nd) {
      if (x == null || isNaN(x)) return '—';
      return Number(x).toLocaleString('ru-RU', { maximumFractionDigits: nd || 0, minimumFractionDigits: nd || 0 });
    },
    esc(s) {
      return String(s == null ? '' : s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
    },
    /** ISO-строка или число секунд -> epoch-секунды. */
    ts(x) {
      if (x == null) return null;
      if (typeof x === 'number') return x;
      const v = Date.parse(x);
      return isNaN(v) ? null : v / 1000;
    },
    severityOf(pred, thr) {
      if (pred == null || isNaN(pred)) return 'unknown';
      if (pred >= thr.critical) return 'critical';
      if (pred >= thr.warning) return 'warning';
      if (pred <= thr.early) return 'early';
      return 'ok';
    },
    /** Количество элементов отсортированного массива, которые <= x. */
    countLE(arr, x) {
      let lo = 0, hi = arr.length;
      while (lo < hi) { const mid = (lo + hi) >> 1; if (arr[mid] <= x) lo = mid + 1; else hi = mid; }
      return lo;
    },
    cssVar(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); },
  };

  /**
   * Сеть маршрутов в общем виде для обоих источников данных.
   * raw = {stops: [{stop_key, lat, lon, name}], routes: [{route_id, tr_id, name, speed_norm_kmh, stops, segments}]}
   */
  class Network {
    constructor(raw) {
      this.stops = new Map(raw.stops.map((s) => [s.stop_key, s]));
      this.routes = raw.routes.slice().sort((a, b) => a.tr_id - b.tr_id);
      this.routeByTr = new Map(this.routes.map((r) => [r.tr_id, r]));
      this.seg = new Map();
      for (const r of this.routes) {
        const patterns = new Map();
        for (const s of r.segments) {
          const pattern = s.route_pattern_id || "";
          if (!patterns.has(pattern)) patterns.set(pattern, new Map());
          patterns.get(pattern).set(`${s.from}-${s.to}`, s);
        }
        this.seg.set(r.tr_id, patterns);
      }
    }
    stopName(key) { const s = this.stops.get(key); return s ? s.name : '—'; }

    /** Отдельные дорожные полилинии между посещениями i..j для одного паттерна. */
    sectionPaths(tr, visits, i, j, routePatternId = null) {
      const patterns = this.seg.get(tr);
      if (!patterns || !visits || i < 0 || j < 0) return [];
      let segs = routePatternId ? patterns.get(routePatternId) : null;
      if (!segs && patterns.size === 1) segs = patterns.values().next().value;
      // With several directions, drawing a segment from another pattern is worse
      // than omitting the alert overlay until the matcher identifies the pattern.
      if (!segs) return [];
      if (i > j) [i, j] = [j, i];
      const paths = [];
      for (let k = i; k < j; k++) {
        const a = visits.stop[k], b = visits.stop[k + 1];
        if (a === b) continue;
        const segment = segs.get(`${a}-${b}`);
        if (segment && segment.path.length > 1) paths.push(segment.path);
      }
      return paths;
    }

    /** Flattened form is used only to calculate camera bounds, never as a line. */
    sectionPath(tr, visits, i, j, routePatternId = null) {
      return this.sectionPaths(tr, visits, i, j, routePatternId).flat();
    }

  }

  window.U = U;
  window.Network = Network;
})();
