/* Экран «Аналитика»: качество прогноза и работа системы. Считаем только по прогнозам,
   факт по которым уже наступил. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const IDS = ['an-mae-time', 'an-scatter', 'an-hist', 'an-confusion', 'an-routes', 'an-horizon', 'an-alert-lead', 'an-calib', 'an-causes',
    'an-day', 'an-flow', 'an-latency', 'an-link'];
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

  /** Вызывается при открытии экрана: графики подстраиваются под размер. */
  function show() {
    ensure();
    setTimeout(() => Object.values(charts).forEach((c) => c.resize()), 0);
  }

  const mean = (a) => (a.length ? a.reduce((x, y) => x + y, 0) / a.length : null);
  const pct = (x) => (x == null ? '—' : `${Math.round(x * 100)}%`);
  const sec = (x) => (x == null ? '—' : `${Math.round(x)} с`);

  /** Общие настройки графиков ECharts. */
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

  const legend = (C, data, bars) => ({ data, top: 0, right: 8, icon: 'roundRect', itemWidth: bars ? 10 : 12, itemHeight: bars ? 10 : 3,
    textStyle: { color: C.text2, fontSize: 11 } });

  /** Пустой график с поясняющей надписью. */
  function empty(chart, C, text) {
    chart.setOption({
      ...base(C), xAxis: { show: false }, yAxis: { show: false }, series: [],
      graphic: { type: 'text', left: 'center', top: 'middle', style: { text, fill: C.muted, fontSize: 12, align: 'center' } },
    }, true);
  }

  /** Пересчитывает все показатели и графики экрана по текущему состоянию. */
  function render(App, s) {
    ensure();
    const setHtml = (id, html) => { if (html !== App.html[id]) { App.html[id] = html; $(id).innerHTML = html; } };
    const C = App.colors;
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const recs = App.src.predictionRecords(now, dataNow);
    const ver = recs.filter((r) => r.outcome != null && r.outcome_at != null && r.outcome_at <= now);
    const m = s.metrics || {};

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
    const late = App.src.kind === 'replay' ? (m.inference_latency_ms_p95 != null ? 'демо-модель, замер при сборке' : 'в воспроизведении не измеряется — см. LIVE') : m.ml_status === 'fallback' ? 'ML недоступен — упрощённый прогноз' : 'признаки + модель';

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
      tile('Проверено прогнозов', U.num(ver.length), `из ${U.num(recs.length)} выпущенных${m.arrival_detector_mae_s != null ? ` · детектор прибытий ±${Math.round(m.arrival_detector_mae_s)} с` : ''}`),
    ].join('');
    setHtml('an-kpis', kpis);

    // Горизонт считаем до планового и до фактического прибытия. Задним числом считаем прогноз,
    // выпущенный, когда ТС уже приехало.
    const planH = recs.map((r) => (r.target_time_begin - r.as_of) / 60);
    const inWin = (x) => x >= 10 && x <= 15;
    const planIn = planH.filter(inWin).length;
    const factH = ver.map((r) => (r.outcome_at - r.as_of) / 60);
    const retroPred = factH.filter((x) => x <= 0).length;
    const incs = App.src.incidentRecords(now, dataNow);
    // Для каждого прибытия берём первый прогноз с риском и смотрим, опоздал ли автобус больше чем
    // на 2 минуты. «До начала» значит, что в момент тревоги ТС ещё шло по графику.
    const firstFlag = new Map();
    for (const r of ver) {
      if (r.pred < 60) continue;
      const key = `${r.tr_id}|${r.target_time_begin}`;
      const f = firstFlag.get(key);
      if (!f || r.as_of < f.as_of) firstFlag.set(key, r);
    }
    const warned = [...firstFlag.values()].filter((r) => r.outcome > 120);
    const alertLead = warned.map((r) => (r.outcome_at - r.as_of) / 60);
    const ahead = warned.filter((r) => (r.cur_dev || 0) < 60);
    const hk = [
      tile('Горизонт прогноза', pct(planH.length ? planIn / planH.length : null),
        `прогнозов выпущено за 10–15 мин до планового прибытия · всего ${U.num(planH.length)}`,
        'Цель прогноза — остановка, плановое прибытие к которой через 10–15 минут от момента прогноза.'),
      tile('До фактического прибытия', factH.length ? `${Math.round(U.median(factH))} мин` : '—',
        `медиана · в окне 10–15 мин: ${pct(factH.length ? factH.filter(inWin).length / factH.length : null)}`,
        'От выпуска прогноза до реального прибытия. Опаздывающее ТС приходит позже плана, поэтому часть выходит за 15 мин.'),
      tile('Тревога заранее', alertLead.length ? `${Math.round(U.median(alertLead))} мин` : '—',
        `до фактического опоздания, медиана по ${U.num(warned.length)} прибытиям`,
        'От первого прогноза риска (≥ 1 мин) по прибытию к остановке до самого прибытия с опозданием > 2 мин.'),
      tile('Предсказано до начала', pct(warned.length ? ahead.length / warned.length : null),
        `${U.num(ahead.length)} из ${U.num(warned.length)} опозданий: тревога, пока ТС шло по графику`,
        'Опоздание предсказано до того, как ТС начало отставать (текущее отклонение в момент тревоги < 1 мин).'),
      tile('Задним числом', U.num(retroPred),
        retroPred ? `прогнозов после прибытия ТС: ${retroPred}` : 'ни одного прогноза после события',
        'Прогноз, выпущенный, когда ТС уже прибыло к целевой остановке.'),
    ].join('');
    setHtml('an-kpis-horizon', hk);

    const L = App.linkLog;
    let drops = 0;
    for (let k = 1; k < L.length; k++) if (L[k].down && !L[k - 1].down) drops++;
    const onLine = s.vehicles.filter((v) => v.route_id && v.status !== 'offline');
    const matched = onLine.filter((v) => v.route_pattern_id);
    const offRoute = onLine.filter((v) => v.off_route).length;
    const nowDown = !!s.down || !!s.backendDown;
    const sk = [
      tile('Задержка обработки', m.inference_latency_ms_p95 != null ? `${U.num(m.inference_latency_ms_p95, 1)} мс` : '—',
        `p95 на один прогноз · ${late}`),
      tile('Поток телеметрии', m.packets_per_min != null ? `${U.num(m.packets_per_min)}` : '—',
        `отметок/мин · ТС на связи ${m.vehicles_live != null ? m.vehicles_live : '—'}`),
      tile('Очередь обработки', m.queue_lag_s != null ? `${U.num(m.queue_lag_s, 1)} с` : '—',
        m.queue_lag_s != null && m.queue_lag_s < 5 ? 'отставание от потока — очередь не копится' : 'отставание обработки от потока',
        'На сколько секунд обработка отстаёт от последней принятой отметки.'),
      tile('Связь с потоком', nowDown ? 'обрыв' : 'в норме',
        `обрывов за сессию ${drops}${m.reconnects ? ` · переподключений ${m.reconnects}` : ''} · ${nowDown ? 'показано последнее известное состояние' : 'данные идут'}`,
        'При обрыве сервис не падает: прогнозы помечаются устаревшими, дашборд показывает последнее известное состояние.'),
      tile('Привязка к маршруту', onLine.length ? `${matched.length} из ${onLine.length}` : '—',
        `ТС с определённым направлением рейса${offRoute ? ` · вне маршрута ${offRoute}` : ''}`,
        'Map matching: направление рейса и положение на его геометрии.'),
    ].join('');
    setHtml('an-kpis-system', sk);
    $('an-sub').textContent = App.src.kind === 'replay'
      ? `Воспроизведение дня ${App.meta.split}: с начала дня до ${U.time(now)}`
      : `Backend: сверенные прогнозы с начала воспроизведения, показатели системы — с открытия страницы`;

    // ось времени от первого прогноза: с backend история начинается с его запуска, а не с полуночи
    const dayStart = recs.length ? Math.floor(recs[0].as_of / 1800) * 1800 : App.meta.day_start || now - 3600;

    // точность по времени суток
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

    // прогноз против факта
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

    // распределение ошибки
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

    // классы прогноза и факта
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

    // точность по маршрутам
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
        legend: legend(C, ['С моделью', 'Без модели'], true),
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

    // гистограммы горизонта
    const band = (lo, hi) => ({ silent: true, itemStyle: { color: C.s1, opacity: 0.08 }, data: [[{ xAxis: lo }, { xAxis: hi }]] });
    const histo = (vals, lo, hi) => {
      const out = [];
      for (let x = lo; x < hi; x++) out.push([x + 0.5, 0]);
      for (const v of vals) {
        const k = Math.max(0, Math.min(out.length - 1, Math.floor(v) - lo));
        out[k][1]++;
      }
      return out;
    };
    if (planH.length) {
      const pl = histo(planH, 0, 30), fa = histo(factH, 0, 30);
      charts['an-horizon'].setOption({
        ...base(C),
        grid: { left: 44, right: 16, top: 28, bottom: 40 },
        legend: legend(C, ['До планового прибытия', 'До фактического прибытия'], true),
        tooltip: { ...base(C).tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
          formatter: (ps) => `<b>${Math.floor(ps[0].value[0])}–${Math.floor(ps[0].value[0]) + 1} мин</b><br>` +
            ps.map((q) => `${q.marker}${q.seriesName}: <b>${U.num(q.value[1])}</b>`).join('<br>') },
        xAxis: axisValue(C, { min: 0, max: 30, interval: 5, name: 'минут до события', nameLocation: 'middle', nameGap: 24 }),
        yAxis: axisValue(C, { name: 'прогнозов' }),
        series: [
          { name: 'До планового прибытия', type: 'bar', data: pl, barWidth: '40%', itemStyle: { color: C.s1, borderRadius: [2, 2, 0, 0] }, markArea: band(10, 15) },
          { name: 'До фактического прибытия', type: 'bar', data: fa, barWidth: '40%', itemStyle: { color: C.s2, borderRadius: [2, 2, 0, 0] } },
        ],
      }, true);
    } else empty(charts['an-horizon'], C, 'Прогнозов пока нет');

    if (alertLead.length) {
      const hA = histo(ahead.map((r) => (r.outcome_at - r.as_of) / 60), 0, 30);
      const hL = histo(warned.filter((r) => (r.cur_dev || 0) >= 60).map((r) => (r.outcome_at - r.as_of) / 60), 0, 30);
      charts['an-alert-lead'].setOption({
        ...base(C),
        grid: { left: 44, right: 16, top: 28, bottom: 40 },
        legend: legend(C, ['ТС ещё шло по графику', 'ТС уже отставало'], true),
        tooltip: { ...base(C).tooltip, trigger: 'axis', axisPointer: { type: 'shadow' },
          formatter: (ps) => `<b>за ${Math.floor(ps[0].value[0])}–${Math.floor(ps[0].value[0]) + 1} мин</b><br>` +
            ps.map((q) => `${q.marker}${q.seriesName}: <b>${q.value[1]}</b>`).join('<br>') },
        xAxis: axisValue(C, { min: 0, max: 30, interval: 5, name: 'минут до опоздания', nameLocation: 'middle', nameGap: 24 }),
        yAxis: axisValue(C, { minInterval: 1, name: 'опозданий' }),
        series: [
          { name: 'ТС ещё шло по графику', type: 'bar', stack: 'a', data: hA, barWidth: '70%', markArea: band(10, 15),
            itemStyle: { color: C.s1, borderColor: C.surface, borderWidth: 1 } },
          { name: 'ТС уже отставало', type: 'bar', stack: 'a', data: hL, itemStyle: { color: C.s2, borderColor: C.surface, borderWidth: 1 } },
        ],
      }, true);
    } else empty(charts['an-alert-lead'], C, 'Предсказанных опозданий пока нет:\nфакт появится после прибытия ТС');

    // вероятность опоздания: доля фактических исходов среди прогнозов с таким же значением
    if (ver.length) {
      const edges = [-Infinity, -60, 0, 60, 120, 180, 300, Infinity];
      const names = ['< −1', '−1…0', '0…1', '1…2', '2…3', '3…5', '> 5'];
      const OUT = [
        ['early', 'Раньше > 1 мин', (o) => o < -60], ['ok', 'По графику', (o) => o >= -60 && o <= 60],
        ['warning', 'Риск 1–2 мин', (o) => o > 60 && o <= 120], ['critical', 'Опоздание > 2 мин', (o) => o > 120],
      ];
      const cnt = names.map(() => ({ n: 0, c: [0, 0, 0, 0] }));
      for (const r of ver) {
        let b = 0;
        while (b < names.length - 1 && r.pred >= edges[b + 1]) b++;
        cnt[b].n++;
        cnt[b].c[OUT.findIndex((o) => o[2](r.outcome))]++;
      }
      const keep = names.map((_, i) => i).filter((i) => cnt[i].n >= 5);
      charts['an-calib'].setOption({
        ...base(C),
        grid: { left: 44, right: 16, top: 44, bottom: 40 },
        legend: legend(C, OUT.map((o) => o[1]), true),
        tooltip: { ...base(C).tooltip, trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps) => {
          const x = cnt[keep[ps[0].dataIndex]];
          return `прогноз <b>${names[keep[ps[0].dataIndex]]} мин</b> · ${U.num(x.n)} прогнозов<br>` +
            ps.slice().reverse().map((q) => `${q.marker}${q.seriesName}: <b>${Math.round(q.value)}%</b>`).join('<br>');
        } },
        xAxis: { type: 'category', data: keep.map((i) => names[i]), axisLine: { lineStyle: { color: C.axis } }, axisTick: { show: false },
          axisLabel: { color: C.text2, fontSize: 11 }, name: 'прогноз отклонения, мин', nameLocation: 'middle', nameGap: 24,
          nameTextStyle: { color: C.muted, fontSize: 10.5 } },
        yAxis: axisValue(C, { min: 0, max: 100, interval: 25, axisLabel: { color: C.muted, fontSize: 10.5, formatter: (v) => `${v}%` } }),
        series: OUT.map(([k, name], oi) => ({
          name, type: 'bar', stack: 'out', barWidth: '62%', itemStyle: { color: C[k], borderColor: C.surface, borderWidth: 1 },
          data: keep.map((i) => (cnt[i].c[oi] / cnt[i].n) * 100),
          label: oi === 3 ? { show: true, position: 'top', color: C.text2, fontSize: 10.5, formatter: (q) => (q.value >= 1 ? `${Math.round(q.value)}%` : '') } : undefined,
        })),
      }, true);
    } else empty(charts['an-calib'], C, 'Нет проверенных прогнозов');

    // причины тревог
    if (incs.length) {
      const byR = new Map();
      for (const i of incs) {
        const key = i.reason_title || 'Причина не определена';
        const x = byR.get(key) || { yes: 0, no: 0, wait: 0 };
        if (i.outcome == null) x.wait++;
        else if (i.kind === 'early' ? i.outcome <= -60 : i.outcome >= 60) x.yes++;
        else x.no++;
        byR.set(key, x);
      }
      const tot = (x) => x.yes + x.no + x.wait;
      const rows = [...byR.entries()].sort((a, b) => tot(a[1]) - tot(b[1]));
      const ser = (key, name, color) => ({ name, type: 'bar', stack: 'r', data: rows.map(([, x]) => x[key]), barWidth: '56%',
        itemStyle: { color, borderColor: C.surface, borderWidth: 1 } });
      charts['an-causes'].setOption({
        ...base(C),
        grid: { left: 200, right: 16, top: 28, bottom: 24 },
        legend: legend(C, ['Подтвердилось', 'Не подтвердилось', 'Ждём факт'], true),
        tooltip: { ...base(C).tooltip, trigger: 'axis', axisPointer: { type: 'shadow' }, formatter: (ps) => {
          const x = rows[ps[0].dataIndex][1], done = x.yes + x.no;
          return `<b>${U.esc(rows[ps[0].dataIndex][0])}</b><br>` + ps.map((q) => `${q.marker}${q.seriesName}: <b>${q.value}</b>`).join('<br>') +
            (done ? `<br>подтверждается: <b>${pct(x.yes / done)}</b>` : '');
        } },
        xAxis: axisValue(C, { minInterval: 1, name: 'тревог' }),
        yAxis: { type: 'category', data: rows.map(([k]) => k), axisLine: { show: false }, axisTick: { show: false },
          axisLabel: { color: C.text2, fontSize: 11, width: 186, overflow: 'truncate' } },
        series: [ser('yes', 'Подтвердилось', C.s1), ser('no', 'Не подтвердилось', C.s2), ser('wait', 'Ждём факт', C.muted)],
      }, true);
    } else empty(charts['an-causes'], C, 'Тревог пока не было');

    // связь и свежесть данных
    if (L.length >= 2) {
      const areas = [];
      for (let k = 0; k < L.length; k++) {
        if (!L[k].down) continue;
        let e = k;
        while (e + 1 < L.length && L[e + 1].down) e++;
        areas.push([{ xAxis: L[k].t * 1000 }, { xAxis: (L[e + 1] ? L[e + 1].t : L[e].t) * 1000 }]);
        k = e;
      }
      charts['an-link'].setOption({
        ...base(C),
        tooltip: { ...base(C).tooltip, trigger: 'axis', formatter: (ps) =>
          `<b>${U.time(ps[0].value[0] / 1000)}</b><br>последняя отметка: <b>${ps[0].value[1] != null ? U.dur(ps[0].value[1]) : '—'}</b> назад` },
        xAxis: axisTime(C, L[0].t, L[L.length - 1].t),
        yAxis: axisValue(C, { min: 0, name: 'с' }),
        series: [{
          type: 'line', step: 'end', symbol: 'none', data: L.map((x) => [x.t * 1000, x.age]), lineStyle: { width: 2, color: C.s1 },
          markArea: { silent: true, itemStyle: { color: C.critical, opacity: 0.15 }, data: areas },
        }],
      }, true);
    } else empty(charts['an-link'], C, 'Данные копятся с открытия страницы');

    // картина дня и работа системы
    const h = App.src.dayHistory(now, dataNow);
    if (h.t.length) {
      const ts = h.t.map((t) => t * 1000);
      const ser = (k) => ({
        name: U.SEV[k].label, type: 'line', stack: 'day', step: 'end', symbol: 'none', data: ts.map((t, i) => [t, h[k][i]]),
        lineStyle: { width: 0 }, areaStyle: { color: C[k], opacity: 0.85 }, itemStyle: { color: C[k] },
      });
      charts['an-day'].setOption({
        ...base(C),
        legend: legend(C, ['critical', 'warning', 'early', 'ok'].map((k) => U.SEV[k].label), true),
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
        ? (ms != null ? `В воспроизведении задержку не измерить во времени:\nзамер демо-модели при сборке — ${U.num(ms, 1)} мс на прогноз`
          : 'В воспроизведении задержка не измеряется:\nреальный замер backend — в режиме LIVE')
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
