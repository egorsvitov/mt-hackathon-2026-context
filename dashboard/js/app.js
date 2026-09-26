/* Диспетчерский дашборд: карта, KPI, инциденты, маршруты, проверка прогнозов, карточка ТС.
   Источник данных — ReplaySource (по умолчанию) или ApiSource (?api=http://host:8000). */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const params = new URLSearchParams(location.search);
  const ALERT = { critical: 1, warning: 1, early: 1 };

  const App = {
    src: null, net: null, meta: null,
    now: 0, playing: true, speed: 60,
    sel: null, tab: 'incidents', filter: null,
    linkDownSince: null, restoredAt: null,
    snap: null, lastTs: 0, lastUi: 0, lastCharts: 0, dirty: true, dragging: false,
    colors: {}, html: {},
  };
  window.App = App;

  // ------------------------------------------------------------------ запуск

  function showError(msg) {
    let el = document.querySelector('.err-overlay');
    if (!el) { el = document.createElement('div'); el.className = 'err-overlay'; document.body.appendChild(el); }
    el.textContent = msg;
    document.body.dataset.error = msg;
  }
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
      try {
        App.src = new ApiSource(api, +params.get('poll') || 2000);
        await App.src.init();
      } catch (e) {
        // Деградация: backend недоступен — работаем по историческим данным.
        App.fallbackReason = e.message || String(e);
        App.src = null;
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
    if (params.get('tab')) App.tab = params.get('tab');
    if (params.get('down') && App.src.kind === 'replay') App.linkDownSince = App.now - 60 * +params.get('down');

    readColors();
    initMap();
    initControls();
    initCharts();
    setTab(App.tab);
    update(true);
    // После первой отрисовки KPI высота карты меняется — пересчитываем масштаб.
    requestAnimationFrame(() => {
      if (App.sel == null) fitNetwork();
      else { const s = App.sel; App.sel = null; select(s); }
    });
    document.body.dataset.ready = '1';
    requestAnimationFrame(loop);
  }

  function loop(ts) {
    const dt = App.lastTs ? Math.min((ts - App.lastTs) / 1000, 1) : 0;
    App.lastTs = ts;
    if (App.dmap) App.dmap.animate(ts);
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

  // ------------------------------------------------------------------ управление

  // Воспроизведение идёт в backend (режим LIVE поверх CSV replay) — управляем им через /demo/*.
  const apiReplay = () => App.src.kind === 'api' && !!App.src.replayInfo;

  function control(action, params) {
    if (App.src.kind !== 'api') return;
    App.src.control(action, params).catch((e) => showError(`Управление воспроизведением: ${e.message || e}`));
  }

  function togglePlay() {
    App.playing = !App.playing;
    syncPlay();
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

  function setTab(tab) {
    App.tab = tab;
    document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === tab));
    for (const t of ['incidents', 'routes', 'verified']) $(`tab-${t}`).hidden = t !== tab;
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

  function initControls() {
    const isReplay = App.src.kind === 'replay';
    const canControl = isReplay || apiReplay();
    $('playback').hidden = !canControl;
    $('btn-link').hidden = !canControl;
    syncPlay();
    setSpeed(App.speed);
    if (App.linkDownSince != null) { $('btn-link').classList.add('on'); $('btn-link').textContent = 'Восстановить связь'; }
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
      // В backend перемотка = перезапуск воспроизведения с нового времени.
      if (apiReplay()) control('start', { t: U.time(+scrub.value), speed: App.playing ? App.speed : 0 });
    });
    $('btn-link').onclick = () => {
      if (apiReplay()) {
        App.apiLinkDown = !App.apiLinkDown;
        control('link', { down: App.apiLinkDown });
        if (!App.apiLinkDown) App.restoredAt = performance.now();
        $('btn-link').classList.toggle('on', App.apiLinkDown);
        $('btn-link').textContent = App.apiLinkDown ? 'Восстановить связь' : 'Обрыв связи';
        return;
      }
      if (App.linkDownSince == null) {
        App.linkDownSince = App.now;
        App.restoredAt = null;
      } else {
        App.linkDownSince = null;
        App.restoredAt = performance.now();
      }
      $('btn-link').classList.toggle('on', App.linkDownSince != null);
      $('btn-link').textContent = App.linkDownSince != null ? 'Восстановить связь' : 'Обрыв связи';
      App.dirty = true;
    };
    $('btn-theme').onclick = () => {
      const cur = document.documentElement.dataset.theme === 'light' ? 'dark' : 'light';
      document.documentElement.dataset.theme = cur;
      try { localStorage.setItem('dash-theme', cur); } catch (e) { /* приватный режим */ }
      readColors();
      App.dmap.setTheme(themeName(), App.colors);
      App.selChanged = true;
      App.dirty = true;
    };
    document.querySelectorAll('#tabs button').forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));
    $('opt-other').onchange = () => { App.dirty = true; };
    $('opt-stops').onchange = () => { App.stopsSig = null; App.dirty = true; };

    document.addEventListener('keydown', (e) => {
      if (e.target.tagName === 'INPUT' && e.target.type !== 'range') return;
      if (e.code === 'Space' && canControl) { e.preventDefault(); togglePlay(); }
      if (e.code === 'Escape' && App.sel != null) select(App.sel);
      if (e.code === 'ArrowRight' && isReplay) jump(App.now + 300);
      if (e.code === 'ArrowLeft' && isReplay) jump(App.now - 300);
    });

    // Делегирование кликов в боковой панели и KPI.
    document.addEventListener('click', (e) => {
      const t = e.target.closest('[data-tr]');
      if (t) { select(+t.dataset.tr); return; }
      const k = e.target.closest('[data-filter]');
      if (k) {
        const f = k.dataset.filter;
        App.filter = App.filter === f ? null : f;
        setTab('routes');
      }
      const x = e.target.closest('[data-action]');
      if (x && x.dataset.action === 'clear-filter') { App.filter = null; App.dirty = true; }
      if (x && x.dataset.action === 'close') select(App.sel);
    });
  }

  // ------------------------------------------------------------------ карта (отрисовка — js/map.js)

  function themeName() {
    return getComputedStyle(document.documentElement).colorScheme === 'dark' ? 'dark' : 'light';
  }

  function initMap() {
    App.dmap = new DashMap('map', App.net, {
      onSelect: (tr) => select(tr),
      onSelectPattern: (id) => selectPattern(id),
      routePatternTooltip: (id) => routePatternTooltip(id),
      vehTooltip: (tr) => vehTooltip(tr),
    });
    if (!App.dmap.init(themeName(), App.colors)) {
      $('map').innerHTML = '<div class="empty">В браузере недоступен WebGL — карта не отображается. Остальные панели работают.</div>';
    }
    $('legend').innerHTML = [
      ['critical', 'Опоздание, > 2 мин'], ['warning', 'Риск, 1–2 мин'], ['ok', 'В графике'],
      ['early', 'Опережение, > 1 мин'], ['unknown', 'Нет прогноза'],
    ].map(([s, t]) => `<div class="row">${U.sevIcon(s, 12)}<span class="line" style="--c:${U.SEV[s].color}"></span>${t}</div>`).join('') +
      '<div class="row extra"><span style="width:12px"></span><span class="line dash"></span>участок без GPS-геометрии</div>' +
      '<div class="row extra"><span style="width:12px"></span><svg width="22" height="12"><circle cx="11" cy="6" r="3.5" fill="var(--unknown)"/></svg>ТС без расписания · контекст</div>' +
      '<div class="row extra"><span style="width:12px"></span><svg width="22" height="12"><circle cx="11" cy="6" r="4.5" fill="none" stroke="var(--text-2)" stroke-width="2.5"/></svg>целевая остановка прогноза</div>';
  }

  function fitNetwork() { App.dmap.fitNetwork(); }

  function vehiclesForPattern(id) {
    if (!App.snap) return [];
    const pattern = App.net.patternById.get(id);
    if (!pattern) return [];
    const active = App.snap.vehicles.filter((v) => v.route_pattern_id === id);
    if (active.length) return active;
    const members = new Set(pattern.tr_ids);
    return App.snap.vehicles.filter((v) => members.has(v.tr_id));
  }

  function selectPattern(id) {
    const vehicles = vehiclesForPattern(id);
    if (!vehicles.length) return;
    vehicles.sort((a, b) => {
      const pa = App.snap.predictions.get(a.tr_id), pb = App.snap.predictions.get(b.tr_id);
      return (pb ? U.SEV[pb.severity].rank : -1) - (pa ? U.SEV[pa.severity].rank : -1);
    });
    select(vehicles[0].tr_id);
  }

  function routePatternTooltip(id) {
    const pattern = App.net.patternById.get(id);
    if (!pattern) return '';
    const vehicles = vehiclesForPattern(id);
    const risks = vehicles.map((v) => App.snap.predictions.get(v.tr_id)).filter(Boolean)
      .sort((a, b) => U.SEV[b.severity].rank - U.SEV[a.severity].rank);
    const ids = vehicles.map((v) => v.tr_id).join(', ');
    return `<b>${U.esc(pattern.name)}</b><br><span class="muted">Маршрутный паттерн · ${vehicles.length} ТС${ids ? `: ${ids}` : ''}</span>` +
      (risks.length ? `<br>${U.sevBadge(risks[0].severity)} максимальный риск ${U.delay(risks[0].prediction_delay_s)}` : '<br><span class="muted">нет активного прогноза</span>');
  }

  function vehTooltip(tr) {
    const s = App.snap;
    const v = s.vehicles.find((x) => x.tr_id === tr);
    if (!v) return '';
    const p = s.predictions.get(tr);
    const r = App.net.routeByTr.get(tr);
    let h = `<b>ТС ${tr}</b>${r ? ` · ${U.esc(r.name)}` : ' · <span class="muted">нет расписания в выгрузке</span>'}<br>` +
      `${U.num(v.speed)} км/ч · отметка ${U.dur(v.data_age_s)} назад`;
    if (p) {
      h += `<br>${U.sevBadge(p.severity)} ${U.delay(p.prediction_delay_s)} к «${U.esc(p.target_stop_name)}» (план ${U.time(p.target_time_begin)})`;
      h += `<br><span class="muted">сейчас отклонение ${U.delay(p.cur_dev_s)}</span>`;
    }
    return h;
  }

  // Устаревшие данные (нет свежих пакетов) — не то же самое, что fallback (нет ML).
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
      ? `<div class="tag"><i>${v.tr_id}</i>${U.delay(p.prediction_delay_s)}${stale ? ' · устар.' : ''}</div>` : '';
    return `<svg width="26" height="26" viewBox="-13 -13 26 26">` +
      `<g transform="rotate(${v.heading || 0})"><path d="M0-12.5 4.5-6.5h-9z" style="fill:${stale ? App.colors.unknown : col}"/></g>` +
      `<circle r="7.5" class="ring" style="fill:${col};stroke:${sel ? App.colors.text : App.colors.page}" stroke-width="${sel ? 3 : 2}" ${stale ? 'stroke-dasharray="3 2" fill-opacity=".45"' : ''}/>` +
      `</svg>${tag}`;
  }

  function renderMap(s) {
    const dm = App.dmap;
    if (!dm || !dm.map) return;

    // Линия принадлежит маршрутному паттерну, а риск агрегируется по всем ТС на ней.
    const activePatterns = new Map(
      s.vehicles.filter((v) => v.route_pattern_id).map((v) => [v.tr_id, v.route_pattern_id]),
    );
    const sev = new Map();
    for (const pattern of App.net.patterns) {
      const predictions = pattern.tr_ids.map((tr) => s.predictions.get(tr)).filter(Boolean);
      predictions.sort((a, b) => U.SEV[b.severity].rank - U.SEV[a.severity].rank);
      sev.set(pattern.route_pattern_id, predictions.length ? predictions[0].severity : 'unknown');
    }
    const selectedVehicle = App.sel == null ? null : s.vehicles.find((v) => v.tr_id === App.sel);
    const selectedPattern = App.sel == null ? null : App.net.patternForTr(
      App.sel, selectedVehicle && selectedVehicle.route_pattern_id,
    );
    dm.setActivePatterns(activePatterns);
    dm.setRoutes(sev, selectedPattern ? selectedPattern.route_pattern_id : null);

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
            html: `<b>${U.esc(tgt.name)}</b><br>цель прогноза ТС ${p.tr_id}: план ${U.time(p.target_time_begin)}, прогноз ${U.delay(p.prediction_delay_s)}`,
          } : null,
        });
      }
      dm.setSections(list);
    }

    // Остановки выбранного маршрута (или всех).
    const allStops = $('opt-stops').checked;
    const stSig = `${App.sel}:${allStops}`;
    if (stSig !== App.stopsSig) {
      App.stopsSig = stSig;
      const route = App.sel != null ? App.net.routeByTr.get(App.sel) : null;
      const keys = allStops ? [...App.net.stops.keys()] : route ? route.stops : [];
      dm.setStops(keys.map((k) => App.net.stops.get(k)).filter(Boolean), allStops);
    }

    // ТС.
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

    document.body.dataset.basemap = dm.basemapStatus + (dm.lastError ? ` · ${dm.lastError}` : '');
    // Подсказка, если подложка не загрузилась.
    const hint = dm.basemapStatus === 'file'
      ? 'Подложка карты доступна при запуске через сервер: python dashboard/serve.py'
      : dm.basemapStatus === 'error' ? 'Нет файла подложки: python dashboard/tools/fetch_basemap.py' : '';
    if (hint !== App.html.mapHint) {
      App.html.mapHint = hint;
      $('map-hint').textContent = hint;
      $('map-hint').hidden = !hint;
    }
  }

  // ------------------------------------------------------------------ шапка, баннер, KPI

  function renderTop(s) {
    $('clock-time').textContent = U.time(App.now, true);
    $('clock-date').textContent = U.date(App.now);
    if (!$('playback').hidden && !App.dragging) $('scrub').value = App.now;
    const badge = $('mode-badge');
    const mm = App.meta.model || {};
    let text;
    if (App.src.kind === 'replay') {
      text = s.down ? 'НЕТ СВЯЗИ · последнее состояние'
        : `${App.fallbackReason ? 'REPLAY (резерв)' : 'REPLAY'} · ${App.meta.split} · ${mm.version || 'модель'}`;
    } else {
      text = s.backendDown ? 'НЕТ СВЯЗИ С BACKEND' : s.down ? 'LIVE · нет потока NDTP' : `LIVE · ${s.metrics.model_version || 'backend'}`;
    }
    badge.innerHTML = `<span class="dot"></span>${U.esc(text)}`;
    badge.classList.toggle('down', !!s.down);
  }

  function renderBanner(s) {
    const b = $('banner');
    let html = '', ok = false;
    if (App.fallbackReason && !s.down) {
      html = `${U.sevIcon('warning')}<span><b>Backend недоступен</b> (${U.esc(App.fallbackReason)}). Показаны исторические данные в режиме воспроизведения.</span>`;
    } else if (App.src.kind === 'replay' && s.down) {
      html = `${U.sevIcon('critical')}<span><b>Нет связи с источником телеметрии ${U.dur(App.now - App.linkDownSince)}.</b> ` +
        `Показано последнее известное состояние на ${U.time(App.linkDownSince, true)}: положения ТС и прогнозы помечены как устаревшие. Сервис продолжает работу и догрузит поток после восстановления.</span>`;
    } else if (s.backendDown) {
      html = `${U.sevIcon('critical')}<span><b>Нет связи с backend</b> (${U.esc(s.backendError || '')}). Показано состояние на ${U.time(s.lastOk, true)}; повтор каждые 2 с.</span>`;
    } else if (App.src.kind === 'api' && s.down) {
      html = `${U.sevIcon('warning')}<span><b>Поток NDTP не поступает</b> с ${U.time(s.metrics.last_packet_at, true)}. Backend работает по последнему известному состоянию.</span>`;
    } else if (App.restoredAt && performance.now() - App.restoredAt < 6000) {
      html = `${U.sevIcon('ok')}<span><b>Связь восстановлена.</b> Поток телеметрии догружен, прогнозы пересчитаны.</span>`;
      ok = true;
    }
    if (html !== App.html.banner) {
      App.html.banner = html;
      b.innerHTML = html;
      b.hidden = !html;
      b.classList.toggle('ok', ok);
    }
  }

  function counts(s) {
    const c = { critical: 0, warning: 0, ok: 0, early: 0, stale: 0 };
    const vById = new Map(s.vehicles.map((v) => [v.tr_id, v]));
    for (const p of s.predictions.values()) {
      c[p.severity] = (c[p.severity] || 0) + 1;
      if (isStale(p, vById.get(p.tr_id))) c.stale++;
    }
    return c;
  }

  function renderKpis(s) {
    const c = counts(s);
    const m = s.metrics || {};
    const inService = s.predictions.size;
    const tile = (sev, label, value, sub) => {
      const hot = value > 0 && (sev === 'critical' || sev === 'warning');
      return `<button class="kpi alert${hot ? ' hot' : ''}" style="--c:${U.SEV[sev].color}" data-filter="${sev}" title="Показать маршруты: ${label}">` +
        `<span class="label">${U.sevIcon(sev, 13)}${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></button>`;
    };
    const pct = (x) => (inService ? `${Math.round((x / inService) * 100)}% ТС` : '—');
    const lastAge = m.last_packet_at ? App.now - m.last_packet_at : null;
    const html = [
      `<div class="kpi"><span class="label">На линии</span><span class="value">${inService}<small style="font-size:13px;color:var(--muted);font-weight:500"> / ${App.net.routes.length}</small></span><span class="sub">ТС с прогнозом</span></div>`,
      tile('critical', 'Опоздание', c.critical, pct(c.critical)),
      tile('warning', 'Риск опоздания', c.warning, pct(c.warning)),
      tile('ok', 'В графике', c.ok, pct(c.ok)),
      tile('early', 'Опережение', c.early, pct(c.early)),
      `<div class="kpi"><span class="label">Точность прогноза</span><span class="value">${m.mae_live_s != null ? `${Math.round(m.mae_live_s)} с` : '—'}</span>` +
      `<span class="sub">MAE live · базовый ${m.mae_baseline_live_s != null ? `${Math.round(m.mae_baseline_live_s)} с` : '—'} · n=${m.n_verified || 0}</span></div>`,
      `<div class="kpi"><span class="label">Инференс</span><span class="value">${m.inference_latency_ms_p95 != null ? `${U.num(m.inference_latency_ms_p95, 1)} мс` : '—'}</span><span class="sub">${m.ml_status === 'fallback' ? 'ML недоступен — fallback' : 'на один прогноз · p95'}</span></div>`,
      `<div class="kpi"><span class="label">Поток телеметрии</span><span class="value">${m.packets_per_min != null ? U.num(m.packets_per_min) : '—'}</span>` +
      `<span class="sub">отметок/мин · ${lastAge != null ? `последняя ${U.dur(lastAge)} назад` : 'нет данных'}${c.stale ? ` · ${c.stale} ТС без свежих данных` : ''}</span></div>`,
    ].join('');
    if (html !== App.html.kpis) { App.html.kpis = html; $('kpis').innerHTML = html; }
  }

  // ------------------------------------------------------------------ боковая панель

  function routeName(tr) {
    const v = App.snap && App.snap.vehicles.find((item) => item.tr_id === tr);
    const pattern = App.net.patternForTr(tr, v && v.route_pattern_id);
    if (pattern) return pattern.name;
    const trip = App.net.routeByTr.get(tr);
    return trip ? trip.name : 'маршрут не восстановлен';
  }

  function incidentCard(i) {
    const active = i.status === 'active';
    const sev = active ? i.severity : i.peak_severity;
    const color = U.SEV[sev] ? U.SEV[sev].color : U.SEV.unknown.color;
    const sel = App.sel === i.tr_id ? ' sel' : '';
    const stale = active && i.prediction_status === 'stale';
    const noMl = active && i.prediction_status === 'fallback';
    const calming = active && sev === 'ok';
    if (!active) {
      let outcome = '<span class="muted">исход ещё неизвестен</span>';
      if (i.outcome_delay_s != null) {
        const err = Math.abs(i.alert_prediction_delay_s - i.outcome_delay_s);
        outcome = `прогноз <b>${U.delay(i.alert_prediction_delay_s)}</b> → факт <b>${U.delay(i.outcome_delay_s)}</b> · ` +
          (err <= 60 ? '<span class="outcome good">✓ ошибка ' + U.dur(err) + '</span>' : `ошибка ${U.dur(err)}`);
      }
      return `<div class="card resolved${sel}" style="--c:${color}" data-tr="${i.tr_id}">` +
        `<div class="head"><div><div class="title">ТС ${i.tr_id}</div><div class="route">${U.esc(routeName(i.tr_id))}</div></div>` +
        `${U.sevBadge(sev, i.kind === 'early' ? 'было опережение' : 'было ' + U.SEV[sev].short.toLowerCase())}</div>` +
        `<div class="outcome">${U.esc(i.alert_target_stop_name || '')}: ${outcome}</div>` +
        `<div class="foot"><span>${U.time(i.first_detected_at)}–${U.time(i.closed_at)}</span><span>закрыт</span></div></div>`;
    }
    const until = i.target_time_begin - App.now;
    const frac = Math.max(0, Math.min(1, until / 900));
    const r = i.suspected_reason;
    const prob = i.late_probability != null ? ` · P(опоздание > 2 мин) ${Math.round(i.late_probability * 100)}%` : '';
    return `<div class="card${sel}${stale ? ' stale' : ''}" style="--c:${color}" data-tr="${i.tr_id}">` +
      `<div class="head"><div><div class="title">ТС ${i.tr_id}</div><div class="route">${U.esc(routeName(i.tr_id))}</div></div>` +
      `<div class="delay num">${U.delay(i.prediction_delay_s)}<small>прогноз отклонения</small></div></div>` +
      `<div>${U.sevBadge(sev, calming ? 'Стабилизируется' : undefined)}` +
      `${stale ? ' <span class="sev" style="color:var(--muted)">· данные устарели</span>' : ''}` +
      `${noMl ? ' <span class="sev" style="color:var(--muted)" title="ML-сервис недоступен">· без ML: прогноз = текущее отклонение</span>' : ''}</div>` +
      `<div class="seg-line">Участок: <b>${U.esc(i.segment.from_stop_name)}</b> → <b>${U.esc(i.segment.to_stop_name)}</b></div>` +
      `<div class="times"><span>План <b>${U.time(i.target_time_begin)}</b></span><span>Прогноз <b>${U.time(i.predicted_arrival)}</b></span>` +
      `<span>${until > 0 ? `до прибытия <b>${U.dur(until)}</b>` : 'плановое время прошло'}</span></div>` +
      `<div class="horizon" title="Сколько осталось до планового прибытия (шкала 15 мин)"><i style="width:${(frac * 100).toFixed(1)}%"></i></div>` +
      (r ? `<div class="reason"><span class="rt">Вероятная причина: ${U.esc(r.title)}.</span> <span class="rd">${U.esc(r.detail || '')}</span></div>` : '') +
      (i.recommendation ? `<div class="rec">${U.esc(i.recommendation)}</div>` : '') +
      `<div class="foot"><span>Обнаружено ${U.time(i.first_detected_at)}${prob}</span><span>обновлено ${U.time(i.updated_at)}</span></div></div>`;
  }

  function renderIncidents(s) {
    const act = s.incidents.filter((i) => i.status === 'active')
      .sort((a, b) => U.SEV[b.severity].rank - U.SEV[a.severity].rank || b.prediction_delay_s - a.prediction_delay_s);
    const res = s.incidents.filter((i) => i.status !== 'active').sort((a, b) => b.closed_at - a.closed_at);
    $('n-inc').textContent = act.length;
    let html = act.length ? act.map(incidentCard).join('')
      : `<div class="empty">${U.sevIcon('ok', 22)}<br>Активных инцидентов нет.<br>Все ТС идут в пределах графика.</div>`;
    if (res.length) html += `<div class="section-title">Закрытые за 15 мин · проверка прогноза</div>` + res.map(incidentCard).join('');
    if (html !== App.html.inc) { App.html.inc = html; $('tab-incidents').innerHTML = html; }
  }

  function renderRoutes(s) {
    const vById = new Map(s.vehicles.map((v) => [v.tr_id, v]));
    const groups = new Map();
    for (const trip of App.net.trips) {
      const v = vById.get(trip.tr_id);
      const pattern = App.net.patternForTr(trip.tr_id, v && v.route_pattern_id);
      const key = pattern ? pattern.route_pattern_id : `trip:${trip.tr_id}`;
      if (!groups.has(key)) groups.set(key, { pattern, name: pattern ? pattern.name : trip.name, rows: [] });
      groups.get(key).rows.push({ trip, p: s.predictions.get(trip.tr_id), v });
    }
    let list = [...groups.values()];
    for (const group of list) {
      group.rows.sort((a, b) => {
        const ra = a.p ? U.SEV[a.p.severity].rank : -2, rb = b.p ? U.SEV[b.p.severity].rank : -2;
        return rb - ra || (b.p ? b.p.prediction_delay_s : 0) - (a.p ? a.p.prediction_delay_s : 0);
      });
      group.worst = group.rows.find((row) => row.p);
    }
    list.sort((a, b) => {
      const ra = a.worst ? U.SEV[a.worst.p.severity].rank : -2;
      const rb = b.worst ? U.SEV[b.worst.p.severity].rank : -2;
      return rb - ra || a.name.localeCompare(b.name, 'ru');
    });
    if (App.filter) {
      list = list.map((group) => ({ ...group, rows: group.rows.filter((row) => row.p && row.p.severity === App.filter) }))
        .filter((group) => group.rows.length);
    }
    $('n-routes').textContent = groups.size;
    let html = App.filter
      ? `<div class="section-title" style="display:flex;justify-content:space-between">Фильтр: ${U.SEV[App.filter].label}<a href="#" data-action="clear-filter" style="color:var(--accent)">сбросить</a></div>` : '';
    html += list.map((group) => {
      const title = `<div class="section-title" style="text-transform:none;letter-spacing:0">${U.esc(group.name)} · ${group.rows.length} ТС</div>`;
      const vehicles = group.rows.map(({ trip, p, v }) => {
        const sev = p ? p.severity : 'unknown';
        const sel = App.sel === trip.tr_id ? ' sel' : '';
        const sub = p ? `${v ? `${U.num(v.speed)} км/ч` : 'нет GPS'} · к «${U.esc(p.target_stop_name)}» ${U.time(p.target_time_begin)}` : 'сейчас без прогноза';
        return `<div class="rrow${sel}" style="--c:${U.SEV[sev].color}" data-tr="${trip.tr_id}">` +
          `<div class="n">ТС ${trip.tr_id}</div><div class="v num">${p ? U.delay(p.prediction_delay_s) : '—'}</div>` +
          `<div class="m">${sub}</div><div>${U.sevBadge(sev, U.SEV[sev].short)}</div></div>`;
      }).join('');
      return title + vehicles;
    }).join('') || '<div class="empty">Нет маршрутных паттернов в этом состоянии</div>';
    if (html !== App.html.routes) { App.html.routes = html; $('tab-routes').innerHTML = html; }
  }

  function renderVerified(s) {
    const m = s.metrics || {};
    const list = s.verified || [];
    $('n-ver').textContent = m.n_verified_grid5 != null ? m.n_verified_grid5 : list.length;
    const off = m.offline_eval;
    let html = `<div class="mae-box">` +
      `<div><b>${m.mae_live_s != null ? Math.round(m.mae_live_s) + ' с' : '—'}</b><span>MAE модели, live</span></div>` +
      `<div><b>${m.mae_baseline_live_s != null ? Math.round(m.mae_baseline_live_s) + ' с' : '—'}</b><span>MAE «прогноз = cur_dev»</span></div>` +
      `<div><b>${U.num(m.n_verified || 0)}</b><span>прогнозов проверено</span></div></div>` +
      `<div class="section-title" style="text-transform:none;letter-spacing:0">Прогноз выпускается за 10–15 мин до планового прибытия и сверяется с фактом, когда ТС доехало.` +
      (off ? ` Офлайн на ${U.esc(off.on)}: модель ${off.mae_model} с, cur_dev ${off.mae_cur_dev} с (n=${off.n}).` : '') + `</div>`;
    if (!list.length) html += '<div class="empty">Проверенных прогнозов пока нет</div>';
    else {
      html += `<table class="tbl"><thead><tr><th>Выпущен</th><th>ТС</th><th>Прибытие</th><th class="r">Прогноз</th><th class="r">Факт</th><th class="r">Ошибка</th></tr></thead><tbody>` +
        list.map((x) => {
          const err = Math.abs(x.prediction_delay_s - x.outcome_delay_s);
          return `<tr class="${err > 120 ? 'flag' : ''}" data-tr="${x.tr_id}" style="cursor:pointer" title="${U.esc(x.target_stop_name)}">` +
            `<td>${U.time(x.as_of)}</td><td>${x.tr_id}</td><td>${U.time(x.target_time_begin)}</td>` +
            `<td class="r">${U.delay(x.prediction_delay_s)}</td><td class="r">${U.delay(x.outcome_delay_s)}</td><td class="r">${U.dur(err)}</td></tr>`;
        }).join('') + '</tbody></table>';
    }
    if (html !== App.html.ver) { App.html.ver = html; $('tab-verified').innerHTML = html; }
  }

  // ------------------------------------------------------------------ карточка ТС (нижняя панель)

  function initCharts() {
    App.devChart = echarts.init($('chart-dev'), null, { renderer: 'canvas' });
    App.spdChart = echarts.init($('chart-speed'), null, { renderer: 'canvas' });
    const ro = new ResizeObserver(() => { App.devChart.resize(); App.spdChart.resize(); });
    ro.observe($('chart-dev'));
    ro.observe($('chart-speed'));
  }

  function axisCommon(C, min, max, stepMin) {
    return {
      type: 'time', min: min * 1000, max: max * 1000, minInterval: (stepMin || 15) * 60000, splitNumber: 5,
      axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
      // Подписи только на «круглых» минутах, иначе ECharts ставит их каждые 5 мин.
      axisLabel: {
        color: C.muted, fontSize: 10.5, hideOverlap: true,
        formatter: (v) => (Math.round(v / 60000) % (stepMin || 15) === 0 ? U.time(v / 1000) : ''),
      },
      splitLine: { show: false },
    };
  }

  function tooltipCommon(C, fmt) {
    return {
      trigger: 'axis', confine: true,
      axisPointer: { type: 'line', lineStyle: { color: C.muted, width: 1 } },
      backgroundColor: C.surface, borderColor: C.border, textStyle: { color: C.text, fontSize: 12 },
      formatter: (ps) => {
        if (!ps.length) return '';
        const t = U.time(ps[0].value[0] / 1000);
        return `<b>${t}</b><br>` + ps.filter((p) => p.seriesType !== 'scatter' || ps.length === 1)
          .map((p) => `${p.marker}${p.seriesName}: <b>${fmt(p.value[1])}</b>`).join('<br>');
      },
    };
  }

  /** Скользящее среднее за window секунд (только прошлые точки). */
  function smooth(series, window) {
    const out = [];
    let lo = 0, sum = 0;
    for (let i = 0; i < series.length; i++) {
      sum += series[i][1];
      while (series[lo][0] < series[i][0] - window) sum -= series[lo++][1];
      out.push([series[i][0], sum / (i - lo + 1)]);
    }
    return out;
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
    if (opening) setTimeout(() => { App.dmap.resize(); App.devChart.resize(); App.spdChart.resize(); }, 0);

    const C = App.colors;
    const p = s.predictions.get(tr);
    const v = s.vehicles.find((x) => x.tr_id === tr);
    const r = App.net.routeByTr.get(tr);
    const visits = App.src.schedule(tr);
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const inc = s.incidents.find((i) => i.tr_id === tr && i.status === 'active');

    // Шапка.
    const sev = p ? p.severity : 'unknown';
    const kv = [];
    if (v) kv.push(`Скорость <b>${U.num(v.speed)} км/ч</b>`, `Данные <b>${U.dur(v.data_age_s)} назад</b>`);
    if (p) {
      kv.push(`Отклонение сейчас <b>${U.delay(p.cur_dev_s)}</b>`);
      kv.push(`Прогноз <b>${U.delay(p.prediction_delay_s)}</b> к «${U.esc(p.target_stop_name)}» · план ${U.time(p.target_time_begin)}, ${p.target_time_begin > now ? 'через ' + U.dur(p.target_time_begin - now) : 'время прошло'}`);
      if (p.late_probability != null) kv.push(`P(опоздание) <b>${Math.round(p.late_probability * 100)}%</b>`);
      kv.push(`<span style="color:var(--muted)">${U.esc(p.status)} · ${U.esc(p.model_version || '')}</span>`);
    } else kv.push('<span style="color:var(--muted)">ТС сейчас не на линии — прогноза нет</span>');
    const head = `<span class="t">ТС ${tr}</span>${U.sevBadge(sev)}<span class="kv">${U.esc(r ? r.name : '')}</span>` +
      kv.map((x) => `<span class="kv">${x}</span>`).join('') +
      `<button class="iconbtn close" data-action="close" title="Закрыть (Esc)" aria-label="Закрыть">✕</button>`;
    if (head !== App.html.dhead) { App.html.dhead = head; $('drawer-head').innerHTML = head; }

    // Линейка рейса.
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
          tm = `${U.time(visits.plan[k])} <span>план</span>`;
        } else {
          let d = null;
          if (p && ti >= 0) {
            if (k === ti) d = p.prediction_delay_s;
            else if (k < ti) {
              const f = (visits.plan[k] - now) / Math.max(1, visits.plan[ti] - now);
              d = p.cur_dev_s + (p.prediction_delay_s - p.cur_dev_s) * Math.max(0, Math.min(1, f));
            } else d = p.prediction_delay_s;
          }
          tm = d == null ? U.time(visits.plan[k]) : `${U.time(visits.plan[k])} → <b>${k === ti ? '' : '≈'}${U.time(visits.plan[k] + d)}</b>`;
        }
        if (k === ti) cls += ' target';
        rowsHtml.push(`<div class="stop ${cls}" style="--c:${U.SEV[sev].color}"><span class="pin"></span><span class="nm" title="${name}">${name}${k === ti ? ' · цель' : ''}</span><span class="tm">${tm}</span></div>`);
        if (k === last) rowsHtml.push(`<div class="stop now"><span class="pin"></span><span class="nm" style="color:var(--accent)">● сейчас ${U.time(now)}</span><span class="tm">${v ? U.num(v.speed) + ' км/ч' : ''}</span></div>`);
      }
      strip = rowsHtml.join('');
    }
    if (strip !== App.html.strip) { App.html.strip = strip; $('strip').innerHTML = strip; }

    // Признаки.
    const src = inc || p;
    let ev = '<div class="empty">Нет прогноза</div>';
    if (src) {
      const reason = inc ? inc.suspected_reason : p.reason;
      const rec = inc ? inc.recommendation : p.recommendation;
      ev = (reason ? `<div class="reason" style="padding:2px 6px 4px"><span class="rt">${U.esc(reason.title)}.</span> <span class="rd">${U.esc(reason.detail || '')}</span></div>` : '') +
        (rec ? `<div class="rec" style="margin:0 0 4px">${U.esc(rec)}</div>` : '') +
        (src.evidence || []).map((e) => {
          const val = e.value == null ? '—' : `${U.num(e.value, e.unit === 'км/ч' ? 1 : 0)} ${e.unit}`;
          const norm = e.norm != null ? ` <small>норма ${U.num(e.norm, 1)}</small>` : '';
          return `<div class="row${e.flag ? ' flag' : ''}"><span class="l">${U.esc(e.label)}</span><span class="v">${val}${norm}</span></div>`;
        }).join('');
    }
    if (ev !== App.html.ev) { App.html.ev = ev; $('evidence').innerHTML = ev; }

    // Графики обновляем не чаще раза в секунду.
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
      tooltip: tooltipCommon(C, (m) => U.delay(m * 60)),
      xAxis: axisCommon(C, now - 5400, now + 1200),
      yAxis: {
        type: 'value', axisLabel: { color: C.muted, fontSize: 10.5, formatter: (x) => (x > 0 ? '+' : '') + x },
        splitLine: { lineStyle: { color: C.grid } }, min: (e) => Math.min(-2, Math.floor(e.min)), max: (e) => Math.max(3, Math.ceil(e.max)),
      },
      series: [
        {
          name: 'Факт', type: 'line', data: h.facts.map(([x, d]) => [x * 1000, d / 60]), symbol: 'circle', symbolSize: 5,
          lineStyle: { width: 2, color: C.s1 }, itemStyle: { color: C.s1 },
          markLine: {
            silent: true, symbol: 'none',
            data: [
              refLine(thr.critical, C.critical, 'опоздание'), refLine(thr.warning, C.warning, 'риск'), refLine(thr.early, C.early, 'опережение'),
              { xAxis: now * 1000, lineStyle: { color: C.text2, type: 'solid', width: 1 }, label: { formatter: 'сейчас', color: C.text2, fontSize: 10, position: 'end' } },
            ],
          },
          markArea: {
            silent: true, itemStyle: { color: C.accent, opacity: 0.07 },
            data: [[{ xAxis: (now + 600) * 1000, label: { show: true, formatter: 'окно 10–15 мин', color: C.muted, fontSize: 10, position: 'insideBottom' } }, { xAxis: (now + 900) * 1000 }]],
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
    const norm = r && r.speed_norm_kmh;
    App.spdChart.setOption({
      animation: false,
      grid: { left: 34, right: 14, top: 12, bottom: 22 },
      tooltip: tooltipCommon(C, (x) => `${U.num(x)} км/ч`),
      xAxis: axisCommon(C, now - 3600, now, 10),
      yAxis: { type: 'value', min: 0, axisLabel: { color: C.muted, fontSize: 10.5 }, splitLine: { lineStyle: { color: C.grid } } },
      series: [{
        name: 'Скорость', type: 'line', data: smooth(h.speed, 60).map(([x, y]) => [x * 1000, y]), showSymbol: false,
        lineStyle: { width: 1.5, color: C.s1 }, itemStyle: { color: C.s1 },
        areaStyle: { color: C.s1, opacity: 0.08 },
        markLine: norm ? {
          silent: true, symbol: 'none',
          data: [{ yAxis: norm, lineStyle: { color: C.muted, type: 'dashed', width: 1 }, label: { formatter: `норма ${U.num(norm)}`, color: C.muted, fontSize: 10, position: 'insideEndTop' } }],
        } : undefined,
      }],
    }, true);
  }

  // ------------------------------------------------------------------ цикл отрисовки

  function update(force) {
    const s = App.src.snapshot(App.now, App.linkDownSince);
    App.snap = s;
    if (force) App.selChanged = true;
    renderTop(s);
    renderBanner(s);
    renderKpis(s);
    renderMap(s);
    if (App.tab === 'incidents') renderIncidents(s);
    else { $('n-inc').textContent = s.incidents.filter((i) => i.status === 'active').length; }
    if (App.tab === 'routes') renderRoutes(s);
    else {
      const active = new Set(s.vehicles.filter((v) => v.route_id)
        .map((v) => (App.net.patternForTr(v.tr_id, v.route_pattern_id) || {}).route_pattern_id || `trip:${v.tr_id}`));
      $('n-routes').textContent = active.size;
    }
    if (App.tab === 'verified') renderVerified(s);
    else $('n-ver').textContent = s.metrics && s.metrics.n_verified_grid5 != null ? s.metrics.n_verified_grid5 : (s.verified || []).length;
    renderDrawer(s);
  }

  boot().catch((e) => showError(e.message || String(e)));
})();
