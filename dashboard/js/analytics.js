/* Экран «Аналитика»: качество прогноза и работа системы.
   Все показатели считаются только по прогнозам, чей факт уже наступил к текущему моменту. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const IDS = ['an-mae-time', 'an-scatter', 'an-hist', 'an-confusion', 'an-routes', 'an-day', 'an-flow', 'an-latency'];
  const charts = {};
  const CLASS_ORDER = ['early', 'ontime', 'late'];
  const CLASS_LABEL = { early: 'Раньше графика', ontime: 'По графику', late: 'Опоздание' };

  function ensure() {
    for (const id of IDS) {
      if (charts[id]) continue;
      charts[id] = echarts.init($(id), null, { renderer: 'canvas' });
      new ResizeObserver(() => charts[id].resize()).observe($(id));
    }
  }

  function show() {
    ensure();
    setTimeout(() => Object.values(charts).forEach((c) => c.resize()), 0);
  }

  const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
  const sec = (x) => (x == null ? '—' : `${Math.round(x)} с`);

  function base(C) {
    return {
      animation: false,
      textStyle: { fontFamily: 'system-ui, -apple-system, "Segoe UI", sans-serif' },
      grid: { left: 44, right: 16, top: 28, bottom: 28 },
      tooltip: {
        confine: true, backgroundColor: C.surface, borderColor: C.border, textStyle: { color: C.text, fontSize: 12 },
      },
    };
  }

  const axisValue = (C, extra) => ({
    type: 'value', axisLabel: { color: C.muted, fontSize: 10.5, formatter: (v) => U.num(v, Number.isInteger(v) ? 0 : 1) },
    splitLine: { lineStyle: { color: C.grid } },
    axisLine: { show: false }, nameTextStyle: { color: C.muted, fontSize: 10.5 }, ...extra,
  });

  const axisTime = (C, min, max) => ({
    type: 'time', min: min * 1000, max: max * 1000, axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
    axisLabel: { color: C.muted, fontSize: 10.5, hideOverlap: true, formatter: (v) => U.time(v / 1000) }, splitLine: { show: false },
  });

  const legend = (C, data) => ({ data, top: 0, right: 8, icon: 'roundRect', itemWidth: 12, itemHeight: 3, textStyle: { color: C.text2, fontSize: 11 } });

  function empty(chart, C, text) {
    chart.setOption({
      ...base(C), xAxis: { show: false }, yAxis: { show: false }, series: [],
      graphic: { type: 'text', left: 'center', top: 'middle', style: { text, fill: C.muted, fontSize: 12, align: 'center' } },
    }, true);
  }

  function render(App, s) {
    ensure();
    const C = App.colors;
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const recs = App.src.predictionRecords(now, dataNow);
    const ver = recs.filter((r) => r.outcome != null && r.outcome_at != null && r.outcome_at <= now);
    const m = s.metrics || {};

    // ------------------------------------------------------------ показатели
    const err = ver.map((r) => r.pred - r.outcome);
    const abs = err.map(Math.abs);
    const baseAbs = ver.map((r) => Math.abs((r.cur_dev || 0) - r.outcome));
    const mae = mean(abs), maeBase = mean(baseAbs);
    const within60 = ver.length ? abs.filter((x) => x <= 60).length / ver.length : null;
    const within120 = ver.length ? abs.filter((x) => x <= 120).length / ver.length : null;
    const lates = ver.filter((r) => r.outcome > 120);
    const caught = lates.filter((r) => r.pred >= 60);
    const alarms = ver.filter((r) => r.pred >= 120);
    const falseAl = alarms.filter((r) => r.outcome < 60);
    const leads = caught.map((r) => (r.outcome_at - r.as_of) / 60);
    const lead = U.median(leads);
    const late = App.src.kind === 'replay' ? 'демо-модель, замер при сборке' : m.ml_status === 'fallback' ? 'ML недоступен — упрощённый прогноз' : 'признаки + модель';

    const tile = (label, value, sub, hint) =>
      `<div class="kpi" title="${U.esc(hint || '')}"><span class="label">${label}</span><span class="value">${value}</span><span class="sub">${sub}</span></div>`;
    const kpis = [
      tile('Средняя ошибка прогноза', sec(mae),
        maeBase != null && mae != null ? `без модели ${sec(maeBase)} · ${maeBase > mae ? `лучше на ${Math.round((1 - mae / maeBase) * 100)}%` : 'не лучше базового'}` : 'пока нет проверенных',
        'MAE: |прогноз − факт| по всем проверенным прогнозам. «Без модели» — прогноз = текущее отклонение.'),
      tile('Точных прогнозов', pct(within60), `ошибка до 1 мин · до 2 мин: ${pct(within120)}`),
      tile('Опоздания пойманы', pct(lates.length ? caught.length / lates.length : null),
        `${caught.length} из ${lates.length} фактических опозданий > 2 мин`, 'Доля фактических опозданий, для которых прогноз показывал риск (≥ 1 мин).'),
      tile('Ложные тревоги', pct(alarms.length ? falseAl.length / alarms.length : null),
        `${falseAl.length} из ${alarms.length} тревог «опоздание»`, 'Прогноз ≥ 2 мин, а по факту отставание меньше 1 мин.'),
      tile('Предупреждение заранее', lead == null ? '—' : `${Math.round(lead)} мин`, 'до фактического прибытия, медиана',
        'Сколько минут между прогнозом риска и фактическим опозданием.'),
      tile('Задержка обработки', m.inference_latency_ms_p95 != null ? `${U.num(m.inference_latency_ms_p95, 1)} мс` : '—',
        `p95 на один прогноз · ${late}`),
      tile('Поток телеметрии', m.packets_per_min != null ? `${U.num(m.packets_per_min)}` : '—',
        `отметок/мин · ТС на связи ${m.vehicles_live != null ? m.vehicles_live : '—'}${m.reconnects ? ` · переподключений ${m.reconnects}` : ''}`),
      tile('Проверено прогнозов', U.num(ver.length), `из ${U.num(recs.length)} выпущенных${m.arrival_detector_mae_s != null ? ` · детектор прибытий ±${Math.round(m.arrival_detector_mae_s)} с` : ''}`),
    ].join('');
    if (kpis !== App.html.anKpis) { App.html.anKpis = kpis; $('an-kpis').innerHTML = kpis; }
    $('an-sub').textContent = App.src.kind === 'replay'
      ? `Воспроизведение дня ${App.meta.split}: с начала дня до ${U.time(now)}`
      : `Backend: сверенные прогнозы с начала воспроизведения, показатели системы — с открытия страницы`;

    // Ось времени — от первого прогноза (в LIVE история начинается с запуска backend, а не с полуночи).
    const dayStart = recs.length ? Math.floor(recs[0].as_of / 1800) * 1800 : App.meta.day_start || now - 3600;

    // ------------------------------------------------------------ точность по времени суток
    if (ver.length) {
      const bucket = 1800, rows = new Map();
      for (let k = 0; k < ver.length; k++) {
        const b = Math.floor(ver[k].as_of / bucket) * bucket;
        const x = rows.get(b) || { e: 0, b: 0, n: 0 };
        x.e += abs[k]; x.b += baseAbs[k]; x.n++;
        rows.set(b, x);
      }
      const pts = [...rows.entries()].sort((a, b) => a[0] - b[0]).filter(([, x]) => x.n >= 5);
      charts['an-mae-time'].setOption({
        ...base(C),
        legend: legend(C, ['С моделью', 'Без модели']),
        tooltip: { ...base(C).tooltip, trigger: 'axis', formatter: (ps) => `<b>${U.time(ps[0].value[0] / 1000)}–${U.time(ps[0].value[0] / 1000 + bucket)}</b><br>` +
          ps.map((q) => `${q.marker}${q.seriesName}: <b>${U.num(q.value[1], 1)} мин</b>`).join('<br>') + `<br><span style="color:${C.muted}">прогнозов: ${q0n(ps, rows)}</span>` },
        xAxis: axisTime(C, dayStart, now),
        yAxis: axisValue(C, { min: 0, name: 'мин' }),
        series: [
          { name: 'С моделью', type: 'line', data: pts.map(([t, x]) => [t * 1000, x.e / x.n / 60]), symbolSize: 6, lineStyle: { width: 2, color: C.s1 }, itemStyle: { color: C.s1 } },
          { name: 'Без модели', type: 'line', data: pts.map(([t, x]) => [t * 1000, x.b / x.n / 60]), symbolSize: 5, lineStyle: { width: 2, color: C.muted }, itemStyle: { color: C.muted } },
        ],
      }, true);
    } else empty(charts['an-mae-time'], C, 'Проверенных прогнозов пока нет:\nфакт появится через 10–15 мин после прогноза');

    // ------------------------------------------------------------ прогноз против факта
    if (ver.length) {
      const pts = ver.slice(-3000).map((r) => [r.outcome / 60, r.pred / 60]);
      let lo = Math.min(-2, ...pts.map((p) => Math.min(p[0], p[1]))), hi = Math.max(4, ...pts.map((p) => Math.max(p[0], p[1])));
      lo = Math.floor(lo / 3) * 3;
      hi = Math.ceil(hi / 3) * 3;
      const diag = (d, name) => ({ type: 'line', data: [[lo, lo + d], [hi, hi + d]], symbol: 'none', silent: true, name,
        lineStyle: { color: d ? C.muted : C.text2, width: 1, type: d ? 'dashed' : 'solid' } });
      charts['an-scatter'].setOption({
        ...base(C),
        grid: { left: 44, right: 16, top: 16, bottom: 40 },
        tooltip: { ...base(C).tooltip, trigger: 'item', formatter: (q) => (q.seriesType === 'scatter' ? `факт <b>${U.delay(q.value[0] * 60)}</b><br>прогноз <b>${U.delay(q.value[1] * 60)}</b>` : '') },
        xAxis: { ...axisValue(C, { min: lo, max: hi, interval: 3, name: 'факт, мин', nameLocation: 'middle', nameGap: 24 }) },
        yAxis: axisValue(C, { min: lo, max: hi, interval: 3, name: 'прогноз', nameLocation: 'end' }),
        series: [
          diag(0, 'точно'), diag(1, '+1 мин'), diag(-1, '−1 мин'),
          { name: 'Прогноз', type: 'scatter', data: pts, symbolSize: 5, itemStyle: { color: C.s1, opacity: 0.35 } },
        ],
      }, true);
    } else empty(charts['an-scatter'], C, 'Нет проверенных прогнозов');

    // ------------------------------------------------------------ распределение ошибки
    if (ver.length) {
      const step = 30, lim = 360, bins = [];
      for (let x = -lim; x < lim; x += step) bins.push({ x, n: 0 });
      for (const e of err) {
        const k = Math.max(0, Math.min(bins.length - 1, Math.floor((e + lim) / step)));
        bins[k].n++;
      }
      charts['an-hist'].setOption({
        ...base(C),
        grid: { left: 44, right: 16, top: 16, bottom: 40 },
        tooltip: { ...base(C).tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
          formatter: (ps) => `ошибка ${U.delay(bins[ps[0].dataIndex].x)}…${U.delay(bins[ps[0].dataIndex].x + step)}<br><b>${ps[0].value}</b> прогнозов (${pct(ps[0].value / ver.length)})` },
        xAxis: { type: 'category', data: bins.map((b) => b.x / 60), axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
          axisLabel: { color: C.muted, fontSize: 10.5, interval: 3, formatter: (v) => `${v > 0 ? '+' : ''}${v}` },
          name: 'прогноз − факт, мин', nameLocation: 'middle', nameGap: 24, nameTextStyle: { color: C.muted, fontSize: 10.5 } },
        yAxis: axisValue(C),
        series: [{ type: 'bar', data: bins.map((b) => b.n), barCategoryGap: '8%', itemStyle: { color: C.s1, borderRadius: [3, 3, 0, 0] } }],
      }, true);
    } else empty(charts['an-hist'], C, 'Нет проверенных прогнозов');

    // ------------------------------------------------------------ классы: прогноз и факт
    if (ver.length) {
      const cm = {};
      for (const r of ver) {
        const k = `${U.classOf(r.pred)}|${U.classOf(r.outcome)}`;
        cm[k] = (cm[k] || 0) + 1;
      }
      const rowTot = {};
      for (const f of CLASS_ORDER) rowTot[f] = CLASS_ORDER.reduce((a, p) => a + (cm[`${p}|${f}`] || 0), 0);
      const data = [];
      CLASS_ORDER.forEach((p, xi) => CLASS_ORDER.forEach((f, yi) => {
        const n = cm[`${p}|${f}`] || 0;
        data.push([xi, yi, n, rowTot[f] ? n / rowTot[f] : 0]);
      }));
      charts['an-confusion'].setOption({
        ...base(C),
        grid: { left: 104, right: 16, top: 16, bottom: 44 },
        tooltip: { ...base(C).tooltip, trigger: 'item', formatter: (q) => `прогноз: <b>${CLASS_LABEL[CLASS_ORDER[q.value[0]]]}</b><br>факт: <b>${CLASS_LABEL[CLASS_ORDER[q.value[1]]]}</b><br>${q.value[2]} прогнозов` },
        xAxis: { type: 'category', data: CLASS_ORDER.map((c) => CLASS_LABEL[c]), name: 'прогноз', nameLocation: 'middle', nameGap: 28,
          nameTextStyle: { color: C.muted, fontSize: 10.5 }, axisLabel: { color: C.text2, fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false }, splitArea: { show: false } },
        yAxis: { type: 'category', data: CLASS_ORDER.map((c) => CLASS_LABEL[c]), name: 'факт', nameTextStyle: { color: C.muted, fontSize: 10.5 },
          axisLabel: { color: C.text2, fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false } },
        visualMap: { show: false, dimension: 3, min: 0, max: 1, inRange: { color: [C.bg, C.s1] } },
        series: [{
          type: 'heatmap', data, itemStyle: { borderColor: C.surface, borderWidth: 2, borderRadius: 4 },
          label: { show: true, fontSize: 12, formatter: (q) => `${U.num(q.value[2])}\n${pct(q.value[3])} факта`, color: C.text },
        }],
      }, true);
    } else empty(charts['an-confusion'], C, 'Нет проверенных прогнозов');

    // ------------------------------------------------------------ точность по маршрутам
    if (ver.length) {
      const byTr = new Map();
      ver.forEach((r, k) => {
        const x = byTr.get(r.tr_id) || { e: 0, b: 0, n: 0 };
        x.e += abs[k]; x.b += baseAbs[k]; x.n++;
        byTr.set(r.tr_id, x);
      });
      const rows = [...byTr.entries()].filter(([, x]) => x.n >= 5).sort((a, b) => a[1].e / a[1].n - b[1].e / b[1].n);
      charts['an-routes'].setOption({
        ...base(C),
        grid: { left: 80, right: 16, top: 28, bottom: 24 },
        legend: legend(C, ['С моделью', 'Без модели']),
        tooltip: { ...base(C).tooltip, trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps) => {
          const tr = rows[ps[0].dataIndex][0], r = App.net.routeByTr.get(tr);
          return `<b>ТС ${tr}</b>${r ? ` · ${U.esc(r.name)}` : ''}<br>` + ps.map((q) => `${q.marker}${q.seriesName}: <b>${U.num(q.value, 1)} мин</b>`).join('<br>') +
            `<br><span style="color:${C.muted}">прогнозов: ${rows[ps[0].dataIndex][1].n}</span>`;
        } },
        xAxis: axisValue(C, { min: 0, name: 'мин' }),
        yAxis: { type: 'category', data: rows.map(([tr]) => `ТС ${tr}`), axisLabel: { color: C.text2, fontSize: 11 }, axisLine: { show: false }, axisTick: { show: false } },
        series: [
          { name: 'С моделью', type: 'bar', data: rows.map(([, x]) => x.e / x.n / 60), barGap: '10%', itemStyle: { color: C.s1, borderRadius: [0, 3, 3, 0] } },
          { name: 'Без модели', type: 'bar', data: rows.map(([, x]) => x.b / x.n / 60), itemStyle: { color: C.muted, borderRadius: [0, 3, 3, 0] } },
        ],
      }, true);
    } else empty(charts['an-routes'], C, 'Нет проверенных прогнозов');

    // ------------------------------------------------------------ картина дня и система
    const h = App.src.dayHistory(now, dataNow);
    if (h.t.length) {
      const ts = h.t.map((t) => t * 1000);
      const ser = (k) => ({
        name: U.SEV[k].label, type: 'line', stack: 'day', step: 'end', symbol: 'none', data: ts.map((t, i) => [t, h[k][i]]),
        lineStyle: { width: 0 }, areaStyle: { color: C[k], opacity: 0.85 }, itemStyle: { color: C[k] },
      });
      charts['an-day'].setOption({
        ...base(C),
        legend: legend(C, ['critical', 'warning', 'early', 'ok'].map((k) => U.SEV[k].label)),
        tooltip: { ...base(C).tooltip, trigger: 'axis', formatter: (ps) => `<b>${U.time(ps[0].value[0] / 1000)}</b><br>` +
          ps.slice().reverse().map((q) => `${q.marker}${q.seriesName}: <b>${q.value[1]}</b>`).join('<br>') },
        xAxis: axisTime(C, h.t[0], now),
        yAxis: axisValue(C, { minInterval: 1, name: 'маршрутов' }),
        series: ['critical', 'warning', 'early', 'ok'].map(ser),
      }, true);
      charts['an-flow'].setOption({
        ...base(C),
        tooltip: { ...base(C).tooltip, trigger: 'axis', formatter: (ps) => `<b>${U.time(ps[0].value[0] / 1000)}</b><br>${U.num(ps[0].value[1])} отметок/мин` },
        xAxis: axisTime(C, h.t[0], now),
        yAxis: axisValue(C, { min: 0 }),
        series: [{ type: 'line', data: ts.map((t, i) => [t, h.packets[i]]), symbol: 'none', lineStyle: { width: 2, color: C.s1 }, areaStyle: { color: C.s1, opacity: 0.08 } }],
      }, true);
    } else {
      empty(charts['an-day'], C, 'Данные появятся через несколько минут');
      empty(charts['an-flow'], C, 'Данные появятся через несколько минут');
    }
    if (h.latency && h.latency.some((x) => x != null)) {
      charts['an-latency'].setOption({
        ...base(C),
        tooltip: { ...base(C).tooltip, trigger: 'axis', formatter: (ps) => `<b>${U.time(ps[0].value[0] / 1000)}</b><br>p95: ${U.num(ps[0].value[1], 2)} мс` },
        xAxis: axisTime(C, h.t[0], now),
        yAxis: axisValue(C, { min: 0, name: 'мс' }),
        series: [{ type: 'line', data: h.t.map((t, i) => [t * 1000, h.latency[i]]), symbol: 'none', lineStyle: { width: 2, color: C.s1 } }],
      }, true);
    } else {
      const ms = m.inference_latency_ms_p95;
      empty(charts['an-latency'], C, App.src.kind === 'replay'
        ? `В воспроизведении задержку не измерить во времени:\nзамер демо-модели при сборке — ${ms != null ? U.num(ms, 1) : '—'} мс на прогноз`
        : 'Данные появятся через полминуты');
    }
  }

  function q0n(ps, rows) {
    const x = rows.get(ps[0].value[0] / 1000);
    return x ? x.n : '—';
  }

  function theme() { /* цвета берутся из App.colors при каждой отрисовке */ }

  window.Views = window.Views || {};
  window.Views.analytics = { show, render, theme };
})();
