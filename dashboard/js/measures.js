/* Меры диспетчера (what-if): выпуск дополнительного автобуса и сокращение стоянок.
   Диалог с параметрами, уведомления; меры применяет и считает backend (/whatif):
   резерв — настоящий автобус линии на карте, для стоянок — опоздание «как есть» и «с мерой». */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const TRIP_GAP_SEC = 300; // пауза в расписании длиннее 5 мин — конец рейса (отстой на конечной)

  const M = {};
  let App = null;
  let current = null; // открытый диалог: {kind, tr}

  // ------------------------------------------------------------------ расчёт по расписанию

  /** Остановка, с которой резерв успевает пойти по графику, и конец текущего рейса. */
  function reservePlan(tr, readyMin, startK) {
    const visits = App.src.schedule(tr);
    if (!visits) return null;
    const s = App.snap || {};
    const p = s.predictions ? s.predictions.get(tr) : null;
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const { n, last } = App._progress(visits, p, now, dataNow);
    const ready = now + readyMin * 60;
    const options = [];
    for (let k = Math.max(0, last + 1); k < n && options.length < 40; k++) {
      if (visits.plan[k] >= ready) options.push(k);
    }
    if (!options.length) return null;
    const k0 = startK != null && options.includes(startK) ? startK : options[0];
    let k1 = k0;
    while (k1 + 1 < n && visits.plan[k1 + 1] - visits.plan[k1] < TRIP_GAP_SEC) k1++;
    return { visits, options, k0, k1, delay: p ? p.prediction_delay_s : null };
  }

  const stopName = (visits, k) => App.net.stopName(visits.stop[k]);

  // ------------------------------------------------------------------ уведомления

  function toast(html, kind = 'info', ms = 8000) {
    const box = $('toasts');
    const el = document.createElement('div');
    el.className = `toast ${kind}`;
    el.innerHTML = `<div class="toast-body">${html}</div><button class="iconbtn" aria-label="Закрыть">✕</button>`;
    el.querySelector('button').onclick = () => el.remove();
    box.prepend(el);
    setTimeout(() => el.classList.add('hide'), ms);
    setTimeout(() => el.remove(), ms + 400);
  }

  // ------------------------------------------------------------------ диалог

  function field(label, control, hint) {
    return `<label class="mf"><span class="mf-l">${label}</span>${control}${hint ? `<span class="mf-h">${hint}</span>` : ''}</label>`;
  }

  function renderReserve() {
    const { tr } = current;
    const ready = +($('m-ready') ? $('m-ready').value : 10);
    const pick = $('m-start') && $('m-start').value !== '' ? +$('m-start').value : null;
    const plan = reservePlan(tr, ready, pick);
    const route = App.net.routeByTr.get(tr);
    const d = plan && plan.delay != null ? plan.delay : null;
    let body = `<p class="m-ctx">Линия <b>${U.esc(route ? route.name : `ТС ${tr}`)}</b>` +
      (d != null ? ` · основной ТС ${tr}: прогноз <b>${U.delay(d)}</b>` : '') + '</p>';
    body += field('Резерв готов к выходу через',
      `<select id="m-ready">${[5, 10, 15, 20, 30].map((x) => `<option value="${x}"${x === ready ? ' selected' : ''}>${x} мин</option>`).join('')}</select>`);
    if (!plan) {
      body += '<p class="m-warn">До конца расписания этого ТС резерв не успевает ни на одну остановку.</p>';
      return { body, ok: false };
    }
    const { visits, options, k0, k1 } = plan;
    body += field('Выход на линию с остановки',
      `<select id="m-start">${options.map((k) => `<option value="${k}"${k === k0 ? ' selected' : ''}>${U.time(visits.plan[k])} · ${U.esc(stopName(visits, k))}</option>`).join('')}</select>`,
      'первая в списке — ближайшая, к которой резерв успевает по графику');
    body += `<div class="m-preview">Резерв пойдёт по графику с «${U.esc(stopName(visits, k0))}» в <b>${U.time(visits.plan[k0])}</b>` +
      ` до конечной «${U.esc(stopName(visits, k1))}» (${U.time(visits.plan[k1])}): <b>${k1 - k0 + 1}</b> остановок.` +
      (d != null && d >= 60 ? ` Основной ТС идёт с опозданием ${U.delay(d)} — на этих остановках пассажиров заберёт резерв.` : '') + '</div>';
    return { body, ok: true, plan };
  }

  function renderDwell() {
    const { tr } = current;
    const route = App.net.routeByTr.get(tr);
    const cut = $('m-cut') ? +$('m-cut').value : 10;
    const scope = document.querySelector('input[name="m-scope"]:checked');
    const sc = scope ? scope.value : 'vehicle';
    const lay = $('m-layover') ? $('m-layover').checked : true;
    let body = `<p class="m-ctx">Линия <b>${U.esc(route ? route.name : `ТС ${tr}`)}</b></p>`;
    body += `<div class="mf"><span class="mf-l">Применить</span><div class="m-radio">` +
      `<label><input type="radio" name="m-scope" value="vehicle"${sc === 'vehicle' ? ' checked' : ''}> к ТС ${tr}</label>` +
      `<label><input type="radio" name="m-scope" value="route"${sc === 'route' ? ' checked' : ''}> ко всей линии</label></div></div>`;
    body += field('Стоянка на каждой остановке',
      `<select id="m-cut">${[5, 10, 15].map((x) => `<option value="${x}"${x === cut ? ' selected' : ''}>короче на ${x} с</option>`).join('')}</select>`,
      'по данным обычная стоянка — 14 с (медиана), у 90% остановок не дольше 35 с');
    body += `<label class="m-check"><input type="checkbox" id="m-layover"${lay ? ' checked' : ''}> Сократить отстой на конечной до 2 мин</label>`;
    return { body, ok: true, params: { scope: sc, cut_s: cut, short_layover: lay } };
  }

  function render() {
    const r = current.kind === 'reserve' ? renderReserve() : renderDwell();
    $('m-body').innerHTML = r.body;
    $('m-apply').disabled = !r.ok;
    current.state = r;
    $('m-body').querySelectorAll('select, input').forEach((el) => (el.onchange = render));
  }

  function open(kind, tr) {
    current = { kind, tr };
    $('m-title').textContent = kind === 'reserve' ? 'Выпустить дополнительный автобус' : 'Сократить время на остановках';
    $('m-apply').textContent = kind === 'reserve' ? 'Выпустить' : 'Применить';
    render();
    const dlg = $('measure-dlg');
    if (!dlg.open) dlg.showModal();
  }

  const available = () => App && App.src.kind === 'api';
  const same = (a, b) => String(a) === String(b);

  /** Действующие меры по данным backend (обновляются каждым опросом). */
  function list() {
    return (App.snap && App.snap.measures) || [];
  }

  async function apply() {
    const { kind, tr, state } = current;
    const btn = $('m-apply');
    btn.disabled = true;
    try {
      const live = App.src.kind === 'api';
      let m;
      if (kind === 'reserve') {
        const { visits, k0 } = state.plan;
        if (live) {
          m = await App.src.send('POST', '/whatif/reserve',
            { tr_id: String(tr), ready_min: +$('m-ready').value, start_visit_id: visits.id ? visits.id[k0] : null });
        } else {
          // Демо-режим без backend: создаём локальную меру, чтобы показать работу диспетчера.
          const name0 = stopName(visits, k0);
          const k1 = state.plan.k1;
          m = { id: Date.now() % 1e9, kind: 'reserve', tr_id: tr, virtual_tr_id: `7${tr}`,
                start: { stop_name: name0, time: new Date(visits.plan[k0] * 1000).toISOString() },
                state: 'к точке выхода', text: `Демо: резерв ТС ${tr} выйдет с «${name0}» и пройдёт рейс по графику`, stops: k1 - k0 + 1 };
        }
        App.logEvent('info', `Выпущен дополнительный автобус ${m.virtual_tr_id} на линию ТС ${tr}. ${m.text}`);
        toast(`<b>Выпущен дополнительный автобус · ТС ${m.virtual_tr_id}</b><br>${U.esc(m.text)}`, 'ok', 12000);
      } else {
        const params = state.params || { scope: 'vehicle', cut_s: 10, short_layover: true };
        if (live) {
          m = await App.src.send('POST', '/whatif/dwell', { tr_id: String(tr), ...params });
        } else {
          // Демо-режим: локальная мера с оценкой эффекта по прогнозу.
          const p = App.snap && App.snap.predictions.get(tr);
          const d = p ? p.prediction_delay_s : null;
          m = { id: Date.now() % 1e9, kind: 'dwell', tr_id: tr, scope: params.scope, cut_s: params.cut_s,
                short_layover: params.short_layover,
                text: d != null ? `Демо: сокращение стоянок на ${params.cut_s} с по ТС ${tr} (прогноз ${U.delay(d)})` : `Демо: сокращение стоянок на ${params.cut_s} с по ТС ${tr}` };
        }
        const who = m.scope === 'route' ? `вся линия ТС ${tr}` : `ТС ${tr}`;
        App.logEvent('info', `Сокращены стоянки (${who}, −${m.cut_s} с${m.short_layover ? ', отстой 2 мин' : ''}): ${m.text}`);
        toast(`<b>Сокращены стоянки · ${U.esc(who)}</b><br>${U.esc(m.text || 'эффект появится, когда будет прогноз')}`, 'info', 12000);
      }
      if (App.snap) App.snap.measures = [...list(), m];
      $('measure-dlg').close();
      App.dirty = true;
    } catch (e) {
      $('m-body').insertAdjacentHTML('beforeend', `<p class="m-warn">Не удалось: ${U.esc(e.message || e)}</p>`);
    } finally {
      btn.disabled = false;
    }
  }

  /** Меры, относящиеся к ТС: его собственные, мера линии, резерв (для самого резерва тоже). */
  function forTr(tr) {
    return list().filter((m) => same(m.tr_id, tr) || same(m.virtual_tr_id, tr) ||
      (m.kind === 'dwell' && (m.vehicles || []).some((v) => same(v.tr_id, tr))));
  }

  /** Прогноз опоздания по остановкам впереди «как есть» и «с мерой» для ТС (или null). */
  function projection(tr) {
    for (const m of list()) {
      if (m.kind !== 'dwell') continue;
      const v = (m.vehicles || []).find((x) => same(x.tr_id, tr));
      if (v) return { m, v, rows: v.rows.map((r) => ({ ...r, t: U.ts(r.time_plan) })) };
    }
    return null;
  }

  /** Резерв, выпущенный на линию, если tr — сам резервный автобус. */
  function reserveOf(tr) {
    return list().find((m) => m.kind === 'reserve' && same(m.virtual_tr_id, tr)) || null;
  }

  async function cancel(id) {
    const m = list().find((x) => x.id === id);
    try {
      if (App.src.kind === 'api') await App.src.send('DELETE', `/whatif/${id}`);
      if (App.snap) App.snap.measures = list().filter((x) => x.id !== id);
      const what = m && m.kind === 'reserve' ? `резерв ${m.virtual_tr_id} снят с линии` : 'сокращение стоянок отменено';
      App.logEvent('info', `Мера отменена: ${what}`);
      toast(`Мера отменена: ${U.esc(what)}`, 'info', 5000);
      App.dirty = true;
    } catch (e) {
      toast(`Не удалось отменить меру: ${U.esc(e.message || e)}`, 'info', 6000);
    }
  }

  function init(app) {
    App = app;
    $('m-apply').onclick = apply;
    $('m-cancel').onclick = () => $('measure-dlg').close();
  }

  Object.assign(M, { init, open, forTr, cancel, toast, list, projection, reserveOf, available });
  window.Measures = M;
})();
