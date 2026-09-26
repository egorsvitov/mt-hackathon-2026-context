/* Приложение: три экрана — «Диспетчерская» (здесь), «Аналитика» (analytics.js), «Журнал» (journal.js).
   Источник данных — ReplaySource (по умолчанию) или ApiSource (?api=http://host:8000/api/v1).
   Диспетчерская показывает только то, что нужно для решения: кто, где, насколько опоздает, почему,
   что делать. Метрики качества и системы — на «Аналитике», подробности — в «Журнале». */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const ALERT = { critical: 1, warning: 1, early: 1 };
  const VIEWS = ['dispatch', 'analytics', 'journal'];

  const App = {
    src: null, net: null, meta: null,
    now: 0, playing: true, speed: 60,
    view: 'dispatch', sel: null, tab: 'attention', filter: null,
    linkDownSince: null, restoredAt: null,
    snap: null, lastTs: 0, lastUi: 0, lastCharts: 0, lastPage: 0, dirty: true, dragging: false,
    colors: {}, html: {},
    ack: new Map(), // incident_id -> когда диспетчер взял в работу
    expanded: new Set(), // раскрытые карточки
    events: [], // журнал событий системы
    prev: {},
  };
  window.App = App;
  window.Views = window.Views || {};

  // ------------------------------------------------------------------ запуск

  function showError(msg) {
    let el = document.querySelector('.err-overlay');
    if (!el) { el = document.createElement('div'); el.className = 'err-overlay'; document.body.appendChild(el); }
    el.textContent = msg;
    document.body.dataset.error = msg;
  }
  App.showError = showError;
  window.addEventListener('error', (e) => showError(`Ошибка: ${e.message} (${(e.filename || '').split('/').pop()}:${e.lineno})`));
  window.addEventListener('unhandledrejection', (e) => showError(`Ошибка: ${e.reason && e.reason.message ? e.reason.message : e.reason}`));

  function loadScript(src) {
    return new Promise((res, rej) => {
      const s = document.createElement('script');
      s.src = src;
      s.onload = res;
      s.onerror = () => rej(new Error(`Не найден ${src}. Соберите данные: python dashboard/tools/build_fixtures.py`));
      document.head.appendChild(s);
    });
  }

  async function boot() {
    // Режим: ?api=<url> или config.js (Docker: DASHBOARD_API); ?replay=1 — принудительно воспроизведение.
    const cfg = window.DASH_CONFIG || {};
    const api = params.get('replay') ? null : params.get('api') || cfg.api;
    if (api) {
      $('status-text').textContent = 'подключение к серверу…';
      try {
        App.src = new ApiSource(api, +params.get('poll') || 2000);
        await App.src.init();
      } catch (e) {
        // Деградация: backend недоступен — работаем по историческим данным.
        App.fallbackReason = e.message || String(e);
        App.src = null;
        watchBackend(api);
      }
    }
    if (!App.src) {
      await loadScript('data/replay.js');
      App.src = new ReplaySource(window.REPLAY_DATA);
    }
    App.net = App.src.network;
    App.meta = App.src.meta;
    App.now = App.src.clock();
    if (App.src.kind === 'api' && App.src.replayInfo) {
      App.speed = App.src.replayInfo.speed || 1;
      App.playing = App.src.replayInfo.speed > 0;
      App.apiLinkDown = !!App.src.replayInfo.link_down;
    }
    if (params.get('t') && App.src.kind === 'replay') {
      const [h, m] = params.get('t').split(':').map(Number);
      const midnight = App.now - ((App.now + 10800) % 86400);
      App.now = midnight + h * 3600 + (m || 0) * 60;
    }
    if (params.get('speed')) App.speed = +params.get('speed');
    if (params.get('paused')) App.playing = false;
    if (params.get('sel')) App.sel = +params.get('sel');
    if (params.get('down') && App.src.kind === 'replay') App.linkDownSince = App.now - 60 * +params.get('down');

    readColors();
    initMap();
    initControls();
    initCharts();
    for (const v of Object.values(window.Views)) if (v.init) v.init(App);
    logEvent('ok', App.src.kind === 'api'
      ? `Дашборд подключён к backend (${App.src.base})`
      : `Дашборд запущен в режиме воспроизведения${App.fallbackReason ? ' — backend недоступен' : ''}`);
    setView(viewFromHash(), true);
    App.ready = true;
    update(true);
    requestAnimationFrame(() => {
      if (App.sel == null) App.dmap.fitNetwork();
      else { const s = App.sel; App.sel = null; select(s); }
    });
    document.body.dataset.ready = '1';
    requestAnimationFrame(loop);
  }

  function loop(ts) {
    const dt = App.lastTs ? Math.min((ts - App.lastTs) / 1000, 1) : 0;
    App.lastTs = ts;
    if (App.view === 'dispatch' && App.dmap) App.dmap.animate(ts);
    if (App.src.kind === 'replay') {
      if (App.playing) {
        App.now += dt * App.speed;
        if (App.now >= App.meta.day_end) { App.now = App.meta.day_end; App.playing = false; syncPlay(); }
      }
    } else {
      App.now = App.src.clock();
    }
    if (App.dirty || ts - App.lastUi > 250) {
      App.lastUi = ts;
      update(App.dirty);
      App.dirty = false;
    }
    requestAnimationFrame(loop);
  }

  function readColors() {
    const v = U.cssVar;
    App.colors = {
      critical: v('--crit'), warning: v('--warn'), early: v('--early'), ok: v('--ok'), unknown: v('--unknown'),
      page: v('--page'), surface: v('--surface-2'), bg: v('--surface-3'), text: v('--text'), text2: v('--text-2'), muted: v('--muted'),
      grid: v('--grid'), axis: v('--axis'), border: v('--border-strong'), s1: v('--s1'), s2: v('--s2'), accent: v('--accent'),
    };
  }
  App.readColors = readColors;

  // ------------------------------------------------------------------ события системы (для «Журнала»)

  function logEvent(kind, text, t) {
    App.events.unshift({ t: t != null ? t : App.now, wall: Date.now(), kind, text });
    if (App.events.length > 1000) App.events.pop();
  }
  App.logEvent = logEvent;

  function trackEvents(s) {
    const P = App.prev;
    const down = !!s.down, bdown = !!s.backendDown, ml = s.metrics ? s.metrics.ml_status : undefined;
    if (P.init) {
      if (down !== P.down && !bdown) {
        logEvent(down ? 'critical' : 'ok', down ? 'Поток телеметрии прервался — показано последнее известное состояние'
          : 'Поток телеметрии восстановлен');
      }
      if (bdown !== P.bdown) logEvent(bdown ? 'critical' : 'ok', bdown ? `Нет связи с backend: ${s.backendError || ''}` : 'Связь с backend восстановлена');
      if (ml && P.ml && ml !== P.ml) {
        logEvent(ml === 'fallback' ? 'warning' : 'ok', ml === 'fallback'
          ? 'ML-сервис недоступен — прогноз по текущему отклонению' : 'ML-сервис снова отвечает');
      }
    }
    const status = P.inc || new Map();
    for (const i of s.incidents) {
      const was = status.get(i.incident_id);
      if (P.init && !was && i.status === 'active') {
        const r = i.suspected_reason ? ` — ${i.suspected_reason.title.toLowerCase()}` : '';
        logEvent(i.severity, `ТС ${i.tr_id}: ${i.kind === 'early' ? 'раньше графика' : 'опоздание'} ${U.delay(i.prediction_delay_s)} к «${i.target_stop_name}»${r}`,
          i.first_detected_at);
      }
      if (P.init && was === 'active' && i.status === 'resolved') {
        logEvent('ok', `ТС ${i.tr_id}: инцидент закрыт, прогноз вернулся в норму`, i.closed_at);
        App.ack.delete(i.incident_id);
      }
      status.set(i.incident_id, i.status);
    }
    Object.assign(P, { init: true, down, bdown, ml, inc: status });
  }

  // ------------------------------------------------------------------ экраны

  function viewFromHash() {
    const h = location.hash.replace('#', '');
    if (VIEWS.includes(h)) return h;
    if (params.get('tab') === 'verified') return 'journal';
    return 'dispatch';
  }

  function setView(v, silent) {
    App.view = v;
    for (const name of VIEWS) $(`view-${name}`).hidden = name !== v;
    document.querySelectorAll('#views-nav a').forEach((a) => a.classList.toggle('on', a.dataset.view === v));
    document.title = `${{ dispatch: 'Диспетчерская', analytics: 'Аналитика', journal: 'Журнал' }[v]} · Предиктор задержек`;
    if (v === 'dispatch') {
      App.sectionSig = null;
      setTimeout(() => { if (App.dmap) App.dmap.resize(); if (App.devChart) App.devChart.resize(); }, 0);
    } else if (window.Views[v] && window.Views[v].show) {
      window.Views[v].show(App);
    }
    App.lastPage = 0;
    if (!silent) App.dirty = true;
  }
  App.setView = setView;
  // До окончания загрузки экран не переключаем: boot сам откроет экран из адреса.
  window.addEventListener('hashchange', () => { if (App.ready) setView(viewFromHash()); });

  /** Backend был недоступен при старте: проверяем раз в 5 с и предлагаем переключиться. */
  function watchBackend(api) {
    const timer = setInterval(async () => {
      try {
        const r = await fetch(`${api.replace(/\/$/, '')}/health/live`, { cache: 'no-store' });
        if (!r.ok) return;
        clearInterval(timer);
        App.backendBack = api;
        logEvent('ok', 'Сервер снова доступен — можно переключиться на живые данные');
        App.dirty = true;
      } catch (e) { /* ещё не поднялся */ }
    }, 5000);
  }

  /** Открыть ТС на диспетчерской (из журнала, аналитики, карты, списка). */
  App.openVehicle = (tr) => {
    if (App.view !== 'dispatch') location.hash = '#dispatch';
    if (App.sel !== tr) select(tr);
  };

  // ------------------------------------------------------------------ управление (кнопка «Демо»)

  // Воспроизведение идёт в backend (режим LIVE поверх CSV replay) — управляем им через /demo/*.
  const apiReplay = () => App.src.kind === 'api' && !!App.src.replayInfo;

  function control(action, prm, text) {
    if (App.src.kind !== 'api') return;
    if (text) logEvent('info', text);
    App.src.control(action, prm).catch((e) => showError(`Управление воспроизведением: ${e.message || e}`));
  }

  function togglePlay() {
    App.playing = !App.playing;
    syncPlay();
    logEvent('info', App.playing ? 'Воспроизведение продолжено' : 'Воспроизведение на паузе');
    if (apiReplay()) control('speed', { speed: App.playing ? App.speed : 0 });
  }

  function syncPlay() {
    const b = $('btn-play');
    b.textContent = App.playing ? '❚❚' : '▶';
    b.setAttribute('aria-label', App.playing ? 'Пауза' : 'Пуск');
  }

  function setSpeed(s) {
    App.speed = s;
    document.querySelectorAll('#speed button').forEach((b) => b.classList.toggle('on', +b.dataset.speed === s));
    if (App.dmap) App.dmap.moveMs = s >= 300 ? 0 : s >= 60 ? 300 : 900;
  }

  function jump(t) {
    App.now = Math.max(App.meta.day_start, Math.min(App.meta.day_end, t));
    if (App.linkDownSince != null && App.linkDownSince > App.now) App.linkDownSince = App.now;
    if (App.dmap) App.dmap.snapNext();
    App.dirty = true;
  }

  function toggleLink() {
    if (apiReplay()) {
      App.apiLinkDown = !App.apiLinkDown;
      control('link', { down: App.apiLinkDown }, App.apiLinkDown ? 'Демо: имитирован обрыв связи' : 'Демо: связь восстановлена');
      if (!App.apiLinkDown) App.restoredAt = performance.now();
    } else {
      if (App.linkDownSince == null) { App.linkDownSince = App.now; App.restoredAt = null; }
      else { App.linkDownSince = null; App.restoredAt = performance.now(); }
      logEvent('info', App.linkDownSince != null ? 'Демо: имитирован обрыв связи' : 'Демо: связь восстановлена');
    }
    syncLink();
    App.dirty = true;
  }

  function syncLink() {
    const down = apiReplay() ? App.apiLinkDown : App.linkDownSince != null;
    $('btn-link').classList.toggle('on', down);
    $('btn-link').textContent = down ? 'Восстановить связь' : 'Обрыв связи';
  }

  function initControls() {
    const isReplay = App.src.kind === 'replay';
    const canControl = isReplay || apiReplay();
    $('playback').hidden = !canControl;
    $('scrub-row').hidden = !canControl;
    $('btn-link').hidden = !canControl;
    syncPlay();
    setSpeed(App.speed);
    syncLink();

    const pop = $('demo-pop');
    const openDemo = (open) => { pop.hidden = !open; $('btn-demo').setAttribute('aria-expanded', String(open)); };
    $('btn-demo').onclick = (e) => { e.stopPropagation(); openDemo(pop.hidden); };
    document.addEventListener('click', (e) => { if (!pop.hidden && !e.target.closest('.demo-wrap')) openDemo(false); });

    $('btn-play').onclick = togglePlay;
    document.querySelectorAll('#speed button').forEach((b) => (b.onclick = () => {
      setSpeed(+b.dataset.speed);
      if (apiReplay() && App.playing) control('speed', { speed: App.speed });
    }));
    const scrub = $('scrub');
    if (canControl) {
      scrub.min = App.meta.day_start;
      scrub.max = App.meta.day_end;
      scrub.step = 60;
    }
    scrub.addEventListener('input', () => {
      App.dragging = true;
      if (isReplay) jump(+scrub.value);
      else $('clock-time').textContent = U.time(+scrub.value, true);
    });
    scrub.addEventListener('change', () => {
      App.dragging = false;
      logEvent('info', `Демо: перемотка на ${U.time(+scrub.value)}`);
      // В backend перемотка = перезапуск воспроизведения с нового времени.
      if (apiReplay()) control('start', { t: U.time(+scrub.value), speed: App.playing ? App.speed : 0 });
    });
    $('btn-link').onclick = toggleLink;
    $('btn-theme').onclick = () => {
      const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
      document.documentElement.dataset.theme = cur;
      try { localStorage.setItem('dash-theme', cur); } catch (e) { /* приватный режим */ }
      readColors();
      App.dmap.setTheme(themeName(), App.colors);
      for (const v of Object.values(window.Views)) if (v.theme) v.theme(App);
      App.selChanged = true;
      App.lastPage = 0;
      App.dirty = true;
    };
    document.querySelectorAll('#tabs button').forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
    $('opt-other').onchange = () => { App.dirty = true; };
    $('opt-stops').onchange = () => { App.stopsSig = null; App.dirty = true; };

    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' && e.target.type !== 'range') return;
      if (e.target.tagName === 'SELECT') return;
      if (e.code === 'Space' && canControl) { e.preventDefault(); togglePlay(); }
      if (e.code === 'Escape' && App.sel != null) select(App.sel);
      if (e.code === 'ArrowRight' && isReplay) jump(App.now + 300);
      if (e.code === 'ArrowLeft' && isReplay) jump(App.now - 300);
    });

    // Делегирование кликов: карточки, кнопки «В работу» / «Подробнее», фильтры сводки.
    document.addEventListener('click', (e) => {
      const ack = e.target.closest('[data-ack]');
      if (ack) {
        const id = ack.dataset.ack;
        if (App.ack.has(id)) App.ack.delete(id);
        else {
          App.ack.set(id, App.now);
          logEvent('info', `Диспетчер взял в работу: ТС ${ack.dataset.tr}`);
        }
        App.dirty = true;
        return;
      }
      const exp = e.target.closest('[data-expand]');
      if (exp) {
        const id = exp.dataset.expand;
        if (App.expanded.has(id)) App.expanded.delete(id); else App.expanded.add(id);
        App.dirty = true;
        return;
      }
      const f = e.target.closest('[data-filter]');
      if (f) {
        App.filter = App.filter === f.dataset.filter ? null : f.dataset.filter;
        setTab('routes');
        return;
      }
      const x = e.target.closest('[data-action]');
      if (x && x.dataset.action === 'clear-filter') { e.preventDefault(); App.filter = null; App.dirty = true; return; }
      if (x && x.dataset.action === 'close') { select(App.sel); return; }
      const t = e.target.closest('[data-tr]');
      if (t && t.closest('#view-dispatch')) select(+t.dataset.tr);
    });
  }

  function setTab(tab) {
    App.tab = tab;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
    for (const t of ['attention', 'routes']) $(`tab-${t}`).hidden = t !== tab;
    App.dirty = true;
  }

  function select(tr) {
    App.sel = App.sel === tr ? null : tr;
    App.selChanged = true;
    App.dirty = true;
    if (App.sel != null) {
      const s = App.snap;
      const v = s && s.vehicles.find((x) => x.tr_id === App.sel);
      const p = s && s.predictions.get(App.sel);
      const visits = App.src.schedule(App.sel);
      let pts = [];
      if (p && visits) {
        const i = visits.pos.get(p.segment.from_stop_id), j = visits.pos.get(p.segment.to_stop_id);
        pts = App.net.sectionPath(App.sel, visits, i, j, v && v.route_pattern_id);
      }
      if (v) pts.push([v.lat, v.lon]);
      // Приближаем после открытия нижней панели, иначе участок уйдёт за её край.
      setTimeout(() => { App.dmap.resize(); App.dmap.focus(pts); }, 120);
    }
  }

  // ------------------------------------------------------------------ карта (отрисовка — js/map.js)

  function themeName() {
    return getComputedStyle(document.documentElement).colorScheme === 'dark' ? 'dark' : 'light';
  }
  App.themeName = themeName;

  function initMap() {
    App.dmap = new DashMap('map', App.net, {
      onSelect: (tr) => select(tr),
      routeTooltip: (tr) => routeTooltip(App.net.routeByTr.get(tr)),
      vehTooltip: (tr) => vehTooltip(tr),
    });
    if (!App.dmap.init(themeName(), App.colors)) {
      $('map').innerHTML = '<div class="empty">В браузере недоступен WebGL — карта не отображается. Остальные панели работают.</div>';
    }
    $('legend').innerHTML = ['critical', 'warning', 'early', 'ok']
      .map((s) => `<span class="row">${U.sevIcon(s, 12)}${U.SEV[s].label}</span>`).join('') +
      '<span class="row extra"><span class="line dash"></span>нет GPS-геометрии</span>';
  }

  function routeTooltip(r) {
    if (!r) return '';
    const p = App.snap && App.snap.predictions.get(r.tr_id);
    return `<b>${U.esc(r.name)}</b><br>` +
      (p ? `${U.sevBadge(p.severity)} ${plainDelay(p.prediction_delay_s)}` : '<span class="muted">сейчас не на линии</span>');
  }

  function vehTooltip(tr) {
    const s = App.snap;
    const v = s.vehicles.find((x) => x.tr_id === tr);
    if (!v) return '';
    const p = s.predictions.get(tr);
    const r = App.net.routeByTr.get(tr);
    let h = `<b>ТС ${tr}</b>${r ? ` · ${U.esc(r.name)}` : ' · <span class="muted">нет расписания</span>'}<br>` +
      `${U.num(v.speed)} км/ч · данные ${U.dur(v.data_age_s)} назад`;
    if (p) h += `<br>${U.sevBadge(p.severity)} ${plainDelay(p.prediction_delay_s)} к «${U.esc(p.target_stop_name)}» в ${U.time(p.predicted_arrival)}`;
    return h;
  }

  // Устаревшие данные (нет свежих пакетов) — не то же самое, что упрощённый прогноз (нет ML).
  function isStale(p, v) {
    return (p && p.status === 'stale') || (v && v.status !== 'live');
  }

  function vehHtml(v, p, sel) {
    const sev = p ? p.severity : 'unknown';
    const col = App.colors[sev];
    const stale = isStale(p, v);
    if (!v.route_id) {
      return `<svg width="12" height="12" viewBox="-6 -6 12 12"><circle r="3.5" style="fill:${App.colors.unknown};stroke:${App.colors.page}" stroke-width="1.5" opacity=".8"/></svg>`;
    }
    const tag = p && (ALERT[p.severity] || sel)
      ? `<div class="tag"><i>${v.tr_id}</i>${U.delay(p.prediction_delay_s)}${stale ? ' · нет данных' : ''}</div>` : '';
    return `<svg width="26" height="26" viewBox="-13 -13 26 26">` +
      `<g transform="rotate(${v.heading || 0})"><path d="M0-12.5 4.5-6.5h-9z" style="fill:${stale ? App.colors.unknown : col}"/></g>` +
      `<circle r="7.5" class="ring" style="fill:${col};stroke:${sel ? App.colors.text : App.colors.page}" stroke-width="${sel ? 3 : 2}" ${stale ? 'stroke-dasharray="3 2" fill-opacity=".45"' : ''}/>` +
      `</svg>${tag}`;
  }

  function renderMap(s) {
    const dm = App.dmap;
    if (!dm || !dm.map) return;
    const sev = new Map();
    for (const r of App.net.routes) {
      const p = s.predictions.get(r.tr_id);
      sev.set(r.tr_id, p ? p.severity : 'unknown');
    }
    const activePatterns = new Map(
      s.vehicles.filter((v) => v.route_pattern_id).map((v) => [v.tr_id, v.route_pattern_id]),
    );
    dm.setActivePatterns(activePatterns);
    dm.setRoutes(sev, App.sel);

    // Проблемные участки: от последней пройденной остановки до целевой.
    // В режиме API расписание подгружается асинхронно, поэтому раз в ~16 с участки пересобираются.
    const alerts = [...s.predictions.values()].filter((p) => ALERT[p.severity] || p.tr_id === App.sel);
    const ssig = alerts.map((p) => `${p.tr_id}:${p.segment.from_stop_id}:${p.segment.to_stop_id}:${p.severity}:${p.status}:${p.prediction_delay_s}:${activePatterns.get(p.tr_id) || ''}`).join('|') +
      `|${App.sel}|${App.src.kind === 'api' ? Date.now() >> 14 : ''}`;
    if (ssig !== App.sectionSig) {
      App.sectionSig = ssig;
      const list = [];
      alerts.sort((x, y) => U.SEV[x.severity].rank - U.SEV[y.severity].rank);
      for (const p of alerts) {
        const visits = App.src.schedule(p.tr_id);
        if (!visits) continue;
        const i = visits.pos.get(p.segment.from_stop_id), j = visits.pos.get(p.segment.to_stop_id);
        const tgt = App.net.stops.get(visits.stop[j]);
        list.push({
          tr: p.tr_id, sev: p.severity, stale: p.status === 'stale', dim: App.sel != null && App.sel !== p.tr_id,
          paths: typeof App.net.sectionPaths === 'function' ? App.net.sectionPaths(p.tr_id, visits, i, j, activePatterns.get(p.tr_id)) : [],
          // Show a rolling forecast target only for the selected vehicle.
          target: p.tr_id === App.sel && tgt ? {
            lat: tgt.lat, lon: tgt.lon,
            html: `<b>${U.esc(tgt.name)}</b><br>ТС ${p.tr_id}: по плану ${U.time(p.target_time_begin)}, ожидается ${U.time(p.predicted_arrival)}`,
          } : null,
        });
      }
      dm.setSections(list);
    }

    const allStops = $('opt-stops').checked;
    const stSig = `${App.sel}:${allStops}`;
    if (stSig !== App.stopsSig) {
      App.stopsSig = stSig;
      const route = App.sel != null ? App.net.routeByTr.get(App.sel) : null;
      const keys = allStops ? [...App.net.stops.keys()] : route ? route.stops : [];
      dm.setStops(keys.map((k) => App.net.stops.get(k)).filter(Boolean), allStops);
    }

    const showOther = $('opt-other').checked;
    const list = [];
    for (const v of s.vehicles) {
      if (!v.route_id && !showOther) continue;
      if (v.route_id && !v.in_service && v.status === 'offline') continue;
      const p = s.predictions.get(v.tr_id);
      const sel = App.sel === v.tr_id;
      const rank = p ? U.SEV[p.severity].rank : -1;
      list.push({
        tr: v.tr_id, lat: v.lat, lon: v.lon, html: vehHtml(v, p, sel), sel, big: !!v.route_id, clickable: !!v.route_id,
        z: sel ? 20 : v.route_id ? rank + 12 : 1,
      });
    }
    dm.setVehicles(list);

    const hint = dm.basemapStatus === 'file'
      ? 'Подложка карты доступна при запуске через сервер: python dashboard/serve.py'
      : dm.basemapStatus === 'error' ? 'Нет файла подложки: python dashboard/tools/fetch_basemap.py' : '';
    document.body.dataset.basemap = dm.basemapStatus;
    if (hint !== App.html.mapHint) {
      App.html.mapHint = hint;
      $('map-hint').textContent = hint;
      $('map-hint').hidden = !hint;
    }
  }

  // ------------------------------------------------------------------ шапка, баннер, сводка

  /** «+2:13» -> «опоздает на 2 мин 13 с» / «раньше на 1 мин». */
  function plainDelay(d) {
    if (d == null || isNaN(d)) return '';
    const a = Math.round(Math.abs(d)), m = Math.floor(a / 60), s = a % 60;
    const txt = m ? `${m} мин${s ? ` ${s} с` : ''}` : `${s} с`;
    if (d >= 60) return `опоздает на ${txt}`;
    if (d <= -60) return `раньше графика на ${txt}`;
    return 'по графику';
  }
  App.plainDelay = plainDelay;

  function renderTop(s) {
    if (!App.dragging) $('clock-time').textContent = U.time(App.now, true);
    $('clock-date').textContent = U.date(App.now);
    if (!$('playback').hidden && !App.dragging) $('scrub').value = App.now;
    const m = s.metrics || {};
    // Свежесть по часам источника: в LIVE часы дашборда между опросами экстраполируются
    // (при ускоренном воспроизведении — на минуты вперёд), поэтому берём now из ответа backend.
    const srcNow = App.src.kind === 'api' && m.now ? m.now : App.now;
    const lastAge = m.last_packet_at ? Math.max(0, srcNow - m.last_packet_at) : null;
    let text, cls;
    if (s.backendDown) { text = 'нет связи с сервером'; cls = 'down'; }
    else if (s.down) { text = `нет данных ${lastAge != null ? U.dur(lastAge) : ''}`; cls = 'down'; }
    else if (lastAge != null && lastAge > 60) { text = `данные ${U.dur(lastAge)} назад`; cls = 'warn'; }
    else { text = lastAge != null ? `данные ${U.dur(lastAge)} назад` : 'данные поступают'; cls = 'ok'; }
    const pill = $('status-pill');
    $('status-text').textContent = text;
    pill.className = `status-pill ${cls}`;

    const mm = App.meta.model || {};
    const mode = App.src.kind === 'replay'
      ? `Воспроизведение дня ${App.meta.split}${App.fallbackReason ? ' (backend недоступен)' : ''} · модель ${mm.version || '—'}`
      : `Backend ${App.src.base} · ${m.mode === 'replay' ? 'воспроизведение CSV' : 'поток NDTP'} · модель ${m.model_version || '—'}`;
    if (mode !== App.html.mode) { App.html.mode = mode; $('mode-badge').textContent = mode; }
  }

  function renderBanner(s) {
    const b = $('banner');
    let html = '', ok = false;
    if (App.backendBack) {
      html = `${U.sevIcon('ok')}<span><b>Сервер снова доступен.</b> Сейчас показаны исторические данные.</span>` +
        `<button class="ghost sm" onclick="location.reload()">Переключиться на сервер</button>`;
      ok = true;
    } else if (App.fallbackReason && !s.down) {
      html = `${U.sevIcon('warning')}<span><b>Сервер недоступен</b> (${U.esc(App.fallbackReason)}). Показаны исторические данные. ` +
        `Запустите backend (<code>cd backend &amp;&amp; .venv\\Scripts\\python -m uvicorn app.main:app --port 8000</code> или <code>docker compose up</code>) — дашборд предложит переключиться.</span>`;
    } else if (App.src.kind === 'replay' && s.down) {
      html = `${U.sevIcon('critical')}<span><b>Нет связи с источником телеметрии ${U.dur(App.now - App.linkDownSince)}.</b> ` +
        `Показано последнее известное состояние на ${U.time(App.linkDownSince, true)}, ТС и прогнозы помечены как устаревшие.</span>`;
    } else if (s.backendDown) {
      html = `${U.sevIcon('critical')}<span><b>Нет связи с сервером.</b> Показано состояние на ${U.time(s.lastOk, true)}; повтор каждые 2 с.</span>`;
    } else if (App.src.kind === 'api' && s.down) {
      html = `${U.sevIcon('warning')}<span><b>Телеметрия не поступает</b> с ${U.time(s.metrics.last_packet_at, true)}. Показано последнее известное состояние.</span>`;
    } else if (App.restoredAt && performance.now() - App.restoredAt < 6000) {
      html = `${U.sevIcon('ok')}<span><b>Связь восстановлена.</b> Данные догружены, прогнозы пересчитаны.</span>`;
      ok = true;
    }
    if (html !== App.html.banner) {
      App.html.banner = html;
      b.innerHTML = html;
      b.hidden = !html;
      b.classList.toggle('ok', ok);
    }
  }

  /** Одна строка сводки: сколько маршрутов на линии и как они распределены по риску. */
  function renderStatus(s) {
    const c = { critical: 0, warning: 0, early: 0, ok: 0 };
    for (const p of s.predictions.values()) if (c[p.severity] != null) c[p.severity]++;
    const total = s.predictions.size;
    const order = ['critical', 'warning', 'early', 'ok'];
    const bar = total ? order.filter((k) => c[k]).map((k) =>
      `<button class="sb-seg" data-filter="${k}" style="--c:${U.SEV[k].color};flex:${c[k]}" title="${U.SEV[k].label}: ${c[k]}"></button>`).join('')
      : '<span class="sb-empty"></span>';
    const labels = order.map((k) =>
      `<button class="sb-item${c[k] ? '' : ' zero'}${App.filter === k ? ' on' : ''}" data-filter="${k}">${U.sevIcon(k, 13)}<b>${c[k]}</b> ${U.SEV[k].label.toLowerCase()}</button>`).join('');
    const att = s.incidents.filter((i) => i.status === 'active' && i.severity !== 'ok');
    const inWork = att.filter((i) => App.ack.has(i.incident_id)).length;
    const html = `<div class="sb-total"><b>${total}</b> из ${App.net.routes.length} маршрутов на линии</div>` +
      `<div class="sb-bar">${bar}</div><div class="sb-items">${labels}</div>` +
      `<div class="sb-att">${att.length ? `Требуют внимания: <b>${att.length - inWork}</b>${inWork ? ` · в работе ${inWork}` : ''}` : 'Все ТС идут по графику'}</div>`;
    if (html !== App.html.status) { App.html.status = html; $('statusbar').innerHTML = html; }
  }

  // ------------------------------------------------------------------ «Требуют внимания» и «Маршруты»

  function routeName(tr) {
    const r = App.net.routeByTr.get(tr);
    return r ? r.name : 'маршрут не восстановлен';
  }

  function attentionCard(i) {
    const sev = i.severity;
    const color = U.SEV[sev].color;
    const sel = App.sel === i.tr_id ? ' sel' : '';
    const stale = i.prediction_status === 'stale';
    const simple = i.prediction_status === 'fallback';
    const calming = sev === 'ok';
    const until = i.target_time_begin - App.now;
    const r = i.suspected_reason;
    const open = App.expanded.has(i.incident_id);
    const conf = U.confidence(i.late_probability);
    const word = calming ? 'стабилизируется' : sev === 'early' ? 'раньше графика' : 'опоздание';
    const why = calming ? 'Прогноз возвращается в норму' : r ? r.title : 'Причина не определена';
    let details = '';
    if (open) {
      const meta = [`Обнаружено ${U.time(i.first_detected_at)}`];
      if (conf && !simple) meta.push(`уверенность ${conf}`);
      if (simple) meta.push('прогноз упрощённый: модель недоступна');
      if (stale) meta.push('данные устарели');
      details = `<div class="a-details">` +
        `<div>Участок: <b>${U.esc(i.segment.from_stop_name)}</b> → <b>${U.esc(i.segment.to_stop_name)}</b></div>` +
        `<div>По плану <b>${U.time(i.target_time_begin)}</b> → ожидается <b>${U.time(i.predicted_arrival)}</b></div>` +
        (r && !calming && r.detail ? `<div class="a-detail">${U.esc(r.detail)}</div>` : '') +
        (i.recommendation && !calming ? `<div class="rec">${U.esc(i.recommendation)}</div>` : '') +
        `<div class="a-meta">${meta.join(' · ')}</div></div>`;
    }
    return `<div class="acard${sel}${stale ? ' stale' : ''}" style="--c:${color}" data-tr="${i.tr_id}">` +
      `<div class="a-top"><span class="a-ic">${U.sevIcon(sev, 16)}</span>` +
      `<div class="a-main"><div class="a-title">ТС ${i.tr_id} <span class="a-route">${U.esc(routeName(i.tr_id))}</span></div>` +
      `<div class="a-when">${until > 0 ? `через <b>${U.dur(until)}</b>` : '<b>сейчас</b>'} · к «${U.esc(i.target_stop_name)}»</div>` +
      `<div class="a-why">${U.esc(why)}${stale ? ' · <span class="muted">нет свежих данных</span>' : ''}</div></div>` +
      `<div class="a-delay num">${U.delay(i.prediction_delay_s)}<small>${word}</small></div></div>` +
      `${details}<div class="a-actions">` +
      `<button class="ghost sm" data-ack="${i.incident_id}" data-tr="${i.tr_id}">В работу</button>` +
      `<button class="ghost sm" data-expand="${i.incident_id}">${open ? 'Свернуть ▴' : 'Подробнее ▾'}</button></div></div>`;
  }

  function workRow(i) {
    return `<div class="wrow" style="--c:${U.SEV[i.severity].color}" data-tr="${i.tr_id}">${U.sevIcon(i.severity, 13)}` +
      `<span class="w-tr">ТС ${i.tr_id}</span><span class="w-d num">${U.delay(i.prediction_delay_s)}</span>` +
      `<span class="w-t">в работе с ${U.time(App.ack.get(i.incident_id))}</span>` +
      `<button class="linkbtn" data-ack="${i.incident_id}" data-tr="${i.tr_id}">вернуть</button></div>`;
  }

  function renderAttention(s) {
    const act = s.incidents.filter((i) => i.status === 'active');
    // Сначала те, где событие раньше и опоздание больше; «стабилизируется» — в конце.
    const score = (i) => (i.severity === 'ok' ? -1e6 : U.SEV[i.severity].rank * 1e4 - Math.max(0, i.target_time_begin - App.now));
    const todo = act.filter((i) => !App.ack.has(i.incident_id)).sort((a, b) => score(b) - score(a));
    const work = act.filter((i) => App.ack.has(i.incident_id));
    $('n-att').textContent = todo.filter((i) => i.severity !== 'ok').length;
    let html = todo.length ? todo.map(attentionCard).join('')
      : `<div class="empty">${U.sevIcon('ok', 22)}<br>Сейчас ничего не требует внимания.<br>Все ТС идут по графику.</div>`;
    if (work.length) html += `<div class="section-title">В работе · ${work.length}</div>` + work.map(workRow).join('');
    if (html !== App.html.att) { App.html.att = html; $('tab-attention').innerHTML = html; }
  }

  function renderRoutes(s) {
    const vById = new Map(s.vehicles.map((v) => [v.tr_id, v]));
    let rows = App.net.routes.map((r) => ({ r, p: s.predictions.get(r.tr_id), v: vById.get(r.tr_id) }));
    rows.sort((a, b) => {
      const ra = a.p ? U.SEV[a.p.severity].rank : -2, rb = b.p ? U.SEV[b.p.severity].rank : -2;
      return rb - ra || (b.p ? b.p.prediction_delay_s : 0) - (a.p ? a.p.prediction_delay_s : 0);
    });
    if (App.filter) rows = rows.filter((x) => x.p && x.p.severity === App.filter);
    $('n-routes').textContent = App.net.routes.length;
    let html = App.filter
      ? `<div class="section-title" style="display:flex;justify-content:space-between">Показаны: ${U.SEV[App.filter].label.toLowerCase()}<a href="#" data-action="clear-filter" style="color:var(--accent)">показать все</a></div>` : '';
    html += rows.map(({ r, p, v }) => {
      const sev = p ? p.severity : 'unknown';
      const sel = App.sel === r.tr_id ? ' sel' : '';
      const sub = p ? `ТС ${r.tr_id} · ${v ? `${U.num(v.speed)} км/ч` : 'нет GPS'} · к «${U.esc(p.target_stop_name)}» в ${U.time(p.predicted_arrival)}`
        : `ТС ${r.tr_id} · сейчас не на линии`;
      return `<div class="rrow${sel}" style="--c:${U.SEV[sev].color}" data-tr="${r.tr_id}">` +
        `<div class="n">${U.esc(r.name)}</div><div class="v num">${p ? U.delay(p.prediction_delay_s) : '—'}</div>` +
        `<div class="m">${sub}</div><div>${U.sevBadge(sev, U.SEV[sev].short)}</div></div>`;
    }).join('') || '<div class="empty">Нет маршрутов в этом состоянии</div>';
    if (html !== App.html.routes) { App.html.routes = html; $('tab-routes').innerHTML = html; }
  }

  // ------------------------------------------------------------------ карточка ТС (нижняя панель)

  function initCharts() {
    App.devChart = echarts.init($('chart-dev'), null, { renderer: 'canvas' });
    new ResizeObserver(() => App.devChart.resize()).observe($('chart-dev'));
  }

  function axisTime(C, min, max, stepMin) {
    return {
      type: 'time', min: min * 1000, max: max * 1000, minInterval: (stepMin || 15) * 60000, splitNumber: 5,
      axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
      axisLabel: {
        color: C.muted, fontSize: 10.5, hideOverlap: true,
        formatter: (v) => (Math.round(v / 60000) % (stepMin || 15) === 0 ? U.time(v / 1000) : ''),
      },
      splitLine: { show: false },
    };
  }

  function renderDrawer(s) {
    const tr = App.sel;
    const drawer = $('drawer');
    if (tr == null) {
      if (!drawer.hidden) { drawer.hidden = true; document.body.classList.remove('drawer-open'); App.dmap.resize(); }
      return;
    }
    const opening = drawer.hidden;
    drawer.hidden = false;
    document.body.classList.add('drawer-open');
    if (opening) setTimeout(() => { App.dmap.resize(); App.devChart.resize(); }, 0);

    const C = App.colors;
    const p = s.predictions.get(tr);
    const v = s.vehicles.find((x) => x.tr_id === tr);
    const r = App.net.routeByTr.get(tr);
    const visits = App.src.schedule(tr);
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const inc = s.incidents.find((i) => i.tr_id === tr && i.status === 'active');

    // Шапка: кто и главное одной фразой.
    const sev = p ? p.severity : 'unknown';
    let line2;
    if (p) {
      const conf = U.confidence(p.late_probability);
      line2 = `${plainDelay(p.prediction_delay_s)[0].toUpperCase() + plainDelay(p.prediction_delay_s).slice(1)} — ` +
        `к «${U.esc(p.target_stop_name)}» в <b>${U.time(p.predicted_arrival)}</b> вместо ${U.time(p.target_time_begin)}` +
        `${p.target_time_begin > now ? ` · через ${U.dur(p.target_time_begin - now)}` : ''}` +
        `${p.status === 'fallback' ? ' · <span class="muted">прогноз упрощённый</span>' : conf ? ` · уверенность ${conf}` : ''}`;
    } else line2 = '<span class="muted">ТС сейчас не на линии — прогноза нет</span>';
    const facts = [];
    if (p && p.cur_dev_s != null) facts.push(`сейчас ${U.delay(p.cur_dev_s)}`);
    if (v) facts.push(`${U.num(v.speed)} км/ч`, `данные ${U.dur(v.data_age_s)} назад`);
    const head = `<div class="dh-1"><span class="t">ТС ${tr}</span>${U.sevBadge(sev)}<span class="kv">${U.esc(r ? r.name : '')}</span>` +
      `<span class="kv muted">${facts.join(' · ')}</span>` +
      `<button class="iconbtn close" data-action="close" title="Закрыть (Esc)" aria-label="Закрыть">✕</button></div>` +
      `<div class="dh-2">${line2}</div>`;
    if (head !== App.html.dhead) { App.html.dhead = head; $('drawer-head').innerHTML = head; }

    // Ближайшие остановки: пройденные — с фактом, впереди — план и ожидаемое время.
    let strip = '<div class="empty">Расписание загружается…</div>';
    if (visits) {
      const n = visits.plan.length;
      let lastFact = -1;
      for (let k = 0; k < n; k++) if (visits.fact[k] != null && visits.fact[k] <= dataNow && visits.plan[k] <= now + 900) lastFact = k;
      const lastPlan = U.countLE(visits.plan, now) - 1;
      const last = Math.max(lastFact, Math.min(lastPlan, n - 1));
      const ti = p ? visits.pos.get(p.target_stop_id) : -1;
      const from = Math.max(0, last - 2);
      const to = Math.min(n - 1, Math.max(ti, last + 1) + 3);
      const rowsHtml = [];
      for (let k = from; k <= to; k++) {
        const name = U.esc(App.net.stopName(visits.stop[k]));
        const factKnown = visits.fact[k] != null && visits.fact[k] <= dataNow;
        let cls = '', tm;
        if (k <= last && factKnown) {
          const d = visits.fact[k] - visits.plan[k];
          cls = 'passed';
          tm = `${U.time(visits.fact[k])} <span class="${d >= 120 ? 'late' : ''}">${U.delay(d)}</span>`;
        } else if (k <= last) {
          cls = 'passed';
          tm = `${U.time(visits.plan[k])}`;
        } else {
          let d = null;
          if (p && ti >= 0) {
            if (k === ti) d = p.prediction_delay_s;
            else if (k < ti) {
              const f = (visits.plan[k] - now) / Math.max(1, visits.plan[ti] - now);
              const c0 = p.cur_dev_s != null ? p.cur_dev_s : 0;
              d = c0 + (p.prediction_delay_s - c0) * Math.max(0, Math.min(1, f));
            } else d = p.prediction_delay_s;
          }
          tm = d == null ? U.time(visits.plan[k]) : `${U.time(visits.plan[k])} → <b>${k === ti ? '' : '≈'}${U.time(visits.plan[k] + d)}</b>`;
        }
        if (k === ti) cls += ' target';
        rowsHtml.push(`<div class="stop ${cls}" style="--c:${U.SEV[sev].color}"><span class="pin"></span><span class="nm" title="${name}">${name}${k === ti ? ' · прогноз' : ''}</span><span class="tm">${tm}</span></div>`);
        if (k === last) rowsHtml.push(`<div class="stop now"><span class="pin"></span><span class="nm" style="color:var(--accent)">● ТС сейчас здесь</span><span class="tm">${U.time(now)}</span></div>`);
      }
      strip = rowsHtml.join('');
    }
    if (strip !== App.html.strip) { App.html.strip = strip; $('strip').innerHTML = strip; }

    // Почему так считаем: гипотеза, рекомендация, признаки.
    const src = inc || p;
    let ev = '<div class="empty">Нет прогноза</div>';
    if (src) {
      const reason = inc ? inc.suspected_reason : p.reason;
      const rec = inc ? inc.recommendation : p.recommendation;
      ev = (reason ? `<div class="reason"><span class="rt">${U.esc(reason.title)}.</span> <span class="rd">${U.esc(reason.detail || '')}</span></div>` : '<div class="reason rd">Причин для тревоги нет.</div>') +
        (rec ? `<div class="rec">${U.esc(rec)}</div>` : '') +
        (src.evidence || []).map((e) => {
          const val = e.value == null ? '—' : `${U.num(e.value, e.unit === 'км/ч' ? 1 : 0)} ${e.unit}`;
          const norm = e.norm != null ? ` <small>норма ${U.num(e.norm, 1)}</small>` : '';
          return `<div class="row${e.flag ? ' flag' : ''}"><span class="l">${U.esc(e.label)}</span><span class="v">${val}${norm}</span></div>`;
        }).join('');
    }
    if (ev !== App.html.ev) { App.html.ev = ev; $('evidence').innerHTML = ev; }

    // График обновляем не чаще раза в секунду.
    const t = performance.now();
    if (!App.selChanged && t - App.lastCharts < 1000) return;
    App.lastCharts = t;
    App.selChanged = false;
    const h = App.src.history(tr, now, dataNow);
    const thr = App.meta.thresholds;
    const cur = p ? [[p.target_time_begin * 1000, p.prediction_delay_s / 60]] : [];
    const refLine = (y, col, name) => ({ yAxis: y / 60, lineStyle: { color: col, type: 'dashed', width: 1, opacity: 0.8 }, label: { formatter: name, color: C.muted, fontSize: 10, position: 'insideStartTop' } });
    App.devChart.setOption({
      animation: false,
      grid: { left: 38, right: 14, top: 12, bottom: 22 },
      tooltip: {
        trigger: 'axis', confine: true, axisPointer: { type: 'line', lineStyle: { color: C.muted, width: 1 } },
        backgroundColor: C.surface, borderColor: C.border, textStyle: { color: C.text, fontSize: 12 },
        formatter: (ps) => (ps.length ? `<b>${U.time(ps[0].value[0] / 1000)}</b><br>` +
          ps.map((q) => `${q.marker}${q.seriesName}: <b>${U.delay(q.value[1] * 60)}</b>`).join('<br>') : ''),
      },
      xAxis: axisTime(C, now - 5400, now + 1200),
      yAxis: {
        type: 'value', axisLabel: { color: C.muted, fontSize: 10.5, formatter: (x) => (x > 0 ? '+' : '') + x },
        splitLine: { lineStyle: { color: C.grid } }, min: (e) => Math.min(-2, Math.floor(e.min)), max: (e) => Math.max(3, Math.ceil(e.max)),
      },
      series: [
        {
          name: 'Было', type: 'line', data: h.facts.map(([x, d]) => [x * 1000, d / 60]), symbol: 'circle', symbolSize: 5,
          lineStyle: { width: 2, color: C.s1 }, itemStyle: { color: C.s1 },
          markLine: {
            silent: true, symbol: 'none',
            data: [
              refLine(thr.critical, C.critical, 'опоздание'), refLine(thr.warning, C.warning, 'риск'), refLine(thr.early, C.early, 'раньше графика'),
              { xAxis: now * 1000, lineStyle: { color: C.text2, type: 'solid', width: 1 }, label: { formatter: 'сейчас', color: C.text2, fontSize: 10, position: 'end' } },
            ],
          },
          markArea: {
            silent: true, itemStyle: { color: C.accent, opacity: 0.07 },
            data: [[{ xAxis: (now + 600) * 1000, label: { show: true, formatter: 'через 10–15 мин', color: C.muted, fontSize: 10, position: 'insideBottom' } }, { xAxis: (now + 900) * 1000 }]],
          },
        },
        {
          name: 'Прогноз', type: 'line', data: h.forecasts.map(([x, d]) => [x * 1000, d / 60]), symbol: 'circle', symbolSize: 5,
          lineStyle: { width: 2, color: C.s2 }, itemStyle: { color: C.s2 },
        },
        {
          name: 'Текущий прогноз', type: 'scatter', data: cur, symbolSize: 12, z: 5,
          itemStyle: { color: C.s2, borderColor: C.surface, borderWidth: 2 },
          label: { show: true, position: 'left', distance: 8, color: C.text, fontWeight: 600, fontSize: 11, formatter: () => (p ? U.delay(p.prediction_delay_s) : '') },
        },
      ],
    }, true);
  }

  // ------------------------------------------------------------------ цикл отрисовки

  function update(force) {
    const s = App.src.snapshot(App.now, App.linkDownSince);
    App.snap = s;
    if (force) App.selChanged = true;
    trackEvents(s);
    renderTop(s);
    renderBanner(s);
    if (App.view === 'dispatch') {
      $('n-routes').textContent = App.net.routes.length;
      renderStatus(s);
      renderMap(s);
      if (App.tab === 'attention') renderAttention(s);
      else {
        $('n-att').textContent = s.incidents.filter((i) => i.status === 'active' && i.severity !== 'ok' && !App.ack.has(i.incident_id)).length;
        renderRoutes(s);
      }
      renderDrawer(s);
    } else if (force || performance.now() - App.lastPage > 1500) {
      // Аналитика и журнал пересчитываются реже: там сотни и тысячи записей.
      App.lastPage = performance.now();
      const v = window.Views[App.view];
      if (v && v.render) v.render(App, s);
    }
  }

  boot().catch((e) => showError(e.message || String(e)));
})();
