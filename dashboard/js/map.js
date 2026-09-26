/* Карта на MapLibre GL.
   Подложка — вырезка OpenStreetMap в локальном файле data/basemap/moscow.pmtiles (стиль Protomaps,
   шрифты и иконки в vendor/protomaps), поэтому карта работает без интернета и без ключей.
   Поверх подложки — наши слои: маршруты (цвет риска), проблемные участки, целевые остановки,
   остановки выбранного маршрута и маркеры ТС. Содержимое подсказок и маркеров задаёт app.js. */
(function () {
  'use strict';

  const BASEMAP_URL = 'data/basemap/moscow.pmtiles';
  const FLAVOR = { dark: 'black', light: 'grayscale' };
  const HIDDEN_BASE_LAYERS = new Set(['pois', 'roads_shields']); // спокойная подложка для диспетчера
  const ATTRIBUTION = '© <a href="https://www.openstreetmap.org/copyright" target="_blank">участники OpenStreetMap</a> · ' +
    '<a href="https://protomaps.com" target="_blank">Protomaps</a>';

  const pageBase = () => location.href.replace(/[#?].*$/, '').replace(/[^/]*$/, '');
  const lngLat = (p) => [p[1], p[0]]; // [lat, lon] -> [lon, lat]
  const fc = (features) => ({ type: 'FeatureCollection', features });

  function buildRoutes(net, activePatterns) {
    const features = [];
    for (const r of net.routes) {
      for (const dashed of [false, true]) {
        const active = activePatterns && activePatterns.get(r.tr_id);
        const lines = r.segments
          .filter((s) => !!s.synthetic === dashed && (!active || s.route_pattern_id === active))
          .map((s) => s.path.map(lngLat));
        if (!lines.length) continue;
        features.push({
          type: 'Feature', id: r.tr_id * 2 + (dashed ? 1 : 0),
          properties: { tr_id: r.tr_id, dashed },
          geometry: { type: 'MultiLineString', coordinates: lines },
        });
      }
    }
    return fc(features);
  }

  class DashMap {
    /**
     * @param el      id или элемент контейнера
     * @param net     Network (util.js)
     * @param h       {onSelect(tr), routeTooltip(tr) -> html, vehTooltip(tr) -> html}
     */
    constructor(el, net, h) {
      this.el = typeof el === 'string' ? document.getElementById(el) : el;
      this.net = net;
      this.h = h;
      this.ready = false;
      this.sel = null;
      this.moveMs = 300;
      this.snap = true;
      this.markers = new Map();
      this.routeState = new Map();
      this.data = { sections: fc([]), targets: fc([]), stops: fc([]) };
      this.activePatterns = new Map();
      this.patternSignature = '';
      this.routes = buildRoutes(net, this.activePatterns);
      // Подложка из PMTiles читается HTTP Range-запросами: с file:// это невозможно.
      this.basemap = location.protocol !== 'file:' && !!window.pmtiles && !!window.basemaps;
      this.basemapStatus = this.basemap ? 'loading' : 'file';
      const b = new maplibregl.LngLatBounds();
      for (const s of net.stops.values()) b.extend([s.lon, s.lat]);
      this.bounds = b.isEmpty() ? new maplibregl.LngLatBounds([37.3, 55.55], [37.9, 55.95]) : b;
    }

    /** Создать карту. false — WebGL недоступен. */
    init(theme, colors) {
      this.theme = theme;
      this.colors = colors;
      if (this.basemap && !DashMap.protocol) {
        DashMap.protocol = new pmtiles.Protocol();
        maplibregl.addProtocol('pmtiles', DashMap.protocol.tile);
      }
      try {
        this.map = new maplibregl.Map({
          container: this.el, style: this.style(), bounds: this.bounds, fitBoundsOptions: { padding: 24 },
          attributionControl: { compact: true }, dragRotate: false, pitchWithRotate: false, touchPitch: false,
          minZoom: 8, maxZoom: 18, fadeDuration: 0,
        });
      } catch (e) {
        this.error = e;
        return false;
      }
      const m = this.map;
      m.touchZoomRotate.disableRotation();
      m.keyboard.disableRotation();
      m.addControl(new maplibregl.NavigationControl({ showCompass: false }), 'top-left');
      this.popup = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: 'tt', offset: 14, maxWidth: '340px' });
      m.on('style.load', () => this.addOverlays());
      m.on('error', (e) => {
        this.lastError = String((e.error && e.error.message) || e.error || e);
        if (e.sourceId === 'protomaps' || /pmtiles|magic/i.test(this.lastError)) this.basemapStatus = 'error';
      });
      m.on('sourcedata', (e) => {
        if (e.sourceId === 'protomaps' && e.isSourceLoaded && this.basemapStatus === 'loading') this.basemapStatus = 'ok';
      });
      new ResizeObserver(() => m.resize()).observe(this.el);
      return true;
    }

    style() {
      const base = pageBase();
      const flavor = FLAVOR[this.theme] || 'black';
      const style = { version: 8, glyphs: `${base}vendor/protomaps/fonts/{fontstack}/{range}.pbf`, sources: {}, layers: [] };
      if (this.basemap) {
        style.sprite = `${base}vendor/protomaps/sprites/${flavor}`;
        style.sources.protomaps = { type: 'vector', url: `pmtiles://${base}${BASEMAP_URL}`, attribution: ATTRIBUTION };
        style.layers = basemaps.layers('protomaps', basemaps.namedFlavor(flavor), { lang: 'ru' })
          .filter((l) => !HIDDEN_BASE_LAYERS.has(l.id));
      } else {
        style.layers = [{ id: 'bg', type: 'background', paint: { 'background-color': this.colors.bg } }];
      }
      return style;
    }

    /** Наши источники и слои; вызывается после каждой смены стиля (тема). */
    addOverlays() {
      const m = this.map, C = this.colors;
      m.addSource('routes', { type: 'geojson', data: this.routes });
      m.addSource('sections', { type: 'geojson', data: this.data.sections });
      m.addSource('targets', { type: 'geojson', data: this.data.targets });
      m.addSource('stops', { type: 'geojson', data: this.data.stops });

      const bySev = (e) => ['match', e, 'critical', C.critical, 'warning', C.warning, 'ok', C.ok, 'early', C.early, C.unknown];
      const fsev = ['coalesce', ['feature-state', 'sev'], 'unknown'];
      const fdim = ['boolean', ['feature-state', 'dim'], false];
      const round = { 'line-cap': 'round', 'line-join': 'round' };
      const selFilter = ['==', ['get', 'tr_id'], this.sel == null ? -1 : this.sel];

      m.addLayer({
        id: 'routes-line', type: 'line', source: 'routes', filter: ['!', ['get', 'dashed']], layout: round,
        paint: {
          'line-color': bySev(fsev),
          'line-width': ['match', fsev, 'critical', 4, 'warning', 3.5, 'early', 3, 2],
          'line-opacity': ['case', fdim, 0.15, ['==', fsev, 'unknown'], 0.3, ['==', fsev, 'ok'], 0.4, 0.9],
        },
      });
      m.addLayer({
        id: 'routes-dash', type: 'line', source: 'routes', filter: ['get', 'dashed'],
        paint: { 'line-color': bySev(fsev), 'line-width': 2, 'line-dasharray': [1.5, 2.5], 'line-opacity': ['case', fdim, 0.15, 0.5] },
      });
      m.addLayer({
        id: 'routes-selected', type: 'line', source: 'routes', filter: ['all', selFilter, ['!', ['get', 'dashed']]], layout: round,
        paint: { 'line-color': bySev(fsev), 'line-width': 5, 'line-opacity': 0.9 },
      });
      // Невидимая широкая линия — чтобы по тонкому маршруту было легко попасть курсором.
      m.addLayer({ id: 'routes-hit', type: 'line', source: 'routes', paint: { 'line-width': 14, 'line-opacity': 0 } });
      m.addLayer({
        id: 'sections-halo', type: 'line', source: 'sections', layout: round,
        paint: { 'line-color': C.page, 'line-width': 11, 'line-opacity': ['case', ['get', 'dim'], 0.2, 0.55] },
      });
      m.addLayer({
        id: 'sections-line', type: 'line', source: 'sections', filter: ['!', ['get', 'stale']], layout: round,
        paint: { 'line-color': bySev(['get', 'sev']), 'line-width': 6, 'line-opacity': ['case', ['get', 'dim'], 0.3, 0.95] },
      });
      m.addLayer({
        id: 'sections-stale', type: 'line', source: 'sections', filter: ['get', 'stale'],
        paint: { 'line-color': bySev(['get', 'sev']), 'line-width': 6, 'line-dasharray': [1.4, 1], 'line-opacity': ['case', ['get', 'dim'], 0.3, 0.9] },
      });
      // Остановки — поверх линии участка, иначе ближайшие прячутся под ней.
      // state: passed — пройдена; next — впереди до цели; target — цель прогноза; after — после цели; other — прочие.
      const st = ['coalesce', ['get', 'state'], 'other'];
      m.addLayer({
        id: 'stops', type: 'circle', source: 'stops',
        paint: {
          'circle-radius': ['match', st, 'target', 7.5, 'next', 5, 'after', 4, 'passed', 3, ['get', 'r']],
          'circle-color': ['match', st, 'target', ['get', 'color'], 'next', C.text, 'after', C.text2, 'passed', C.muted, C.surface],
          'circle-stroke-color': ['match', st, 'other', C.text2, C.page],
          'circle-stroke-width': ['match', st, 'target', 2.5, 'next', 2, 'other', 1.5, 1],
          'circle-opacity': ['match', st, 'other', 0.7, 1],
          'circle-stroke-opacity': ['match', st, 'other', 0.7, 1],
        },
      });
      m.addLayer({
        id: 'stop-labels', type: 'symbol', source: 'stops', filter: ['has', 'label'],
        layout: {
          'text-field': ['get', 'label'], 'text-font': ['Noto Sans Medium'], 'text-size': ['match', st, 'target', 12.5, 11],
          'text-anchor': 'left', 'text-offset': [0.9, 0], 'text-optional': true, 'text-padding': 1,
        },
        paint: { 'text-color': ['match', st, 'target', C.text, C.text2], 'text-halo-color': C.page, 'text-halo-width': 1.8 },
      });
      m.addLayer({
        id: 'targets', type: 'circle', source: 'targets',
        paint: {
          'circle-radius': 6, 'circle-color': C.surface, 'circle-stroke-width': 3, 'circle-stroke-color': bySev(['get', 'sev']),
          'circle-opacity': ['case', ['get', 'dim'], 0.4, 1], 'circle-stroke-opacity': ['case', ['get', 'dim'], 0.4, 1],
        },
      });

      for (const [id, st] of this.routeState) m.setFeatureState({ source: 'routes', id }, st);
      if (!this.eventsBound) this.bindEvents();
      this.ready = true;
    }

    bindEvents() {
      const m = this.map;
      this.eventsBound = true;
      const hover = {
        'routes-hit': (f) => this.h.routeTooltip(f.properties.tr_id),
        targets: (f) => f.properties.html,
        stops: (f) => `<b>${U.esc(f.properties.name)}</b>${f.properties.tip ? `<br>${U.esc(f.properties.tip)}` : ''}`,
      };
      for (const [id, html] of Object.entries(hover)) {
        m.on('mousemove', id, (e) => {
          if (this.hoverVehicle) return;
          m.getCanvas().style.cursor = id === 'stops' ? '' : 'pointer';
          this.popup.setLngLat(e.lngLat).setHTML(html(e.features[0])).addTo(m);
        });
        m.on('mouseleave', id, () => { m.getCanvas().style.cursor = ''; if (!this.hoverVehicle) this.popup.remove(); });
      }
      m.on('click', (e) => {
        const layers = ['targets', 'routes-hit'].filter((id) => m.getLayer(id));
        const f = m.queryRenderedFeatures(e.point, { layers })[0];
        if (f) this.h.onSelect(f.properties.tr_id);
      });
    }

    setSource(id, data) {
      this.data[id] = data;
      if (this.ready && this.map.getSource(id)) this.map.getSource(id).setData(data);
    }

    setActivePatterns(patterns) {
      const signature = [...patterns].sort((a, b) => a[0] - b[0]).map((x) => `${x[0]}:${x[1] || ''}`).join('|');
      if (signature === this.patternSignature) return;
      this.patternSignature = signature;
      this.activePatterns = patterns;
      this.routes = buildRoutes(this.net, patterns);
      this.routeState.clear();
      this.setSource('routes', this.routes);
    }

    /** sev: Map(tr_id -> severity); sel — выбранное ТС или null. */
    setRoutes(sev, sel) {
      if (!this.map) return;
      if (sel !== this.sel) {
        this.sel = sel;
        if (this.ready) this.map.setFilter('routes-selected', ['all', ['==', ['get', 'tr_id'], sel == null ? -1 : sel], ['!', ['get', 'dashed']]]);
      }
      for (const r of this.net.routes) {
        const st = { sev: sev.get(r.tr_id) || 'unknown', dim: sel != null && sel !== r.tr_id };
        const key = `${st.sev}:${st.dim}`;
        for (const id of [r.tr_id * 2, r.tr_id * 2 + 1]) {
          if (this.routeState.get(id) && this.routeState.get(id)._k === key) continue;
          this.routeState.set(id, { ...st, _k: key });
          if (this.ready) this.map.setFeatureState({ source: 'routes', id }, st);
        }
      }
    }

    /** list: [{tr, path: [[lat, lon]], sev, stale, dim, target: {lat, lon, html}}] */
    setSections(list) {
      const lines = [], points = [];
      for (const s of list) {
        const paths = s.paths || (s.path ? [s.path] : []);
        for (const path of paths) {
          if (path.length > 1) {
            lines.push({ type: 'Feature', properties: { tr_id: s.tr, sev: s.sev, stale: !!s.stale, dim: !!s.dim }, geometry: { type: 'LineString', coordinates: path.map(lngLat) } });
          }
        }
        if (s.target) {
          points.push({ type: 'Feature', properties: { tr_id: s.tr, sev: s.sev, dim: !!s.dim, html: s.target.html }, geometry: { type: 'Point', coordinates: [s.target.lon, s.target.lat] } });
        }
      }
      this.setSource('sections', fc(lines));
      this.setSource('targets', fc(points));
    }

    /** list: [{lat, lon, name}]; small — мельче, когда показаны все остановки сети. */
    /** list: [{lat, lon, name, state?, label?, tip?, color?}]; small — мельче, когда показаны все остановки сети. */
    setStops(list, small) {
      this.setSource('stops', fc(list.map((s) => {
        const props = { name: s.name, r: small ? 2.5 : 3.5, state: s.state || 'other' };
        if (s.label) props.label = s.label;
        if (s.tip) props.tip = s.tip;
        if (s.color) props.color = s.color;
        return { type: 'Feature', properties: props, geometry: { type: 'Point', coordinates: [s.lon, s.lat] } };
      })));
    }

    /** list: [{tr, lat, lon, html, z, sel, big, clickable}] — маркеры ТС (HTML поверх карты). */
    setVehicles(list) {
      if (!this.map) return;
      const now = performance.now();
      const seen = new Set();
      for (const v of list) {
        seen.add(v.tr);
        let mk = this.markers.get(v.tr);
        if (!mk) {
          const el = document.createElement('div');
          el.className = `veh${v.big ? '' : ' small'}`;
          const marker = new maplibregl.Marker({ element: el, anchor: 'center' }).setLngLat([v.lon, v.lat]).addTo(this.map);
          mk = { marker, el, html: null, from: [v.lon, v.lat], to: [v.lon, v.lat], cur: [v.lon, v.lat], t0: 0, done: true };
          const tr = v.tr;
          el.addEventListener('mouseenter', () => {
            this.hoverVehicle = tr;
            this.popup.setLngLat(mk.cur).setHTML(this.h.vehTooltip(tr)).addTo(this.map);
          });
          el.addEventListener('mouseleave', () => { this.hoverVehicle = null; this.popup.remove(); });
          if (v.clickable) el.addEventListener('click', (e) => { e.stopPropagation(); this.h.onSelect(tr); });
          this.markers.set(v.tr, mk);
        }
        if (mk.to[0] !== v.lon || mk.to[1] !== v.lat) {
          mk.from = this.snap || !this.moveMs ? [v.lon, v.lat] : mk.cur.slice();
          mk.to = [v.lon, v.lat];
          mk.t0 = now;
          mk.done = false;
        }
        if (mk.html !== v.html) { mk.el.innerHTML = v.html; mk.html = v.html; }
        mk.el.style.zIndex = v.z;
        mk.el.classList.toggle('sel', !!v.sel);
      }
      for (const [tr, mk] of this.markers) {
        if (!seen.has(tr)) { mk.marker.remove(); this.markers.delete(tr); }
      }
      this.snap = false;
    }

    /** Плавное движение маркеров между отметками; вызывается в каждом кадре. */
    animate(now) {
      for (const mk of this.markers.values()) {
        if (mk.done) continue;
        const k = this.moveMs ? Math.min(1, (now - mk.t0) / this.moveMs) : 1;
        mk.cur = [mk.from[0] + (mk.to[0] - mk.from[0]) * k, mk.from[1] + (mk.to[1] - mk.from[1]) * k];
        mk.marker.setLngLat(mk.cur);
        mk.done = k >= 1;
      }
    }

    /** Следующее обновление — без анимации (перемотка). */
    snapNext() { this.snap = true; }

    setTheme(theme, colors) {
      if (!this.map || theme === this.theme && colors === this.colors) return;
      this.theme = theme;
      this.colors = colors;
      this.ready = false;
      this.popup.remove();
      this.map.setStyle(this.style(), { diff: false });
    }

    fitNetwork() {
      if (!this.map) return;
      this.map.resize();
      this.map.fitBounds(this.bounds, { padding: 24, animate: false });
    }

    /** Показать участок: points — [[lat, lon], ...]. */
    focus(points) {
      if (!this.map || !points.length) return;
      const b = new maplibregl.LngLatBounds();
      for (const p of points) b.extend(lngLat(p));
      this.map.fitBounds(b, { padding: 80, maxZoom: 15, duration: 600 });
    }

    resize() { if (this.map) this.map.resize(); }
  }

  window.DashMap = DashMap;
})();
