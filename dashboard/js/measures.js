/* Меры диспетчера (what-if): выпуск дополнительного автобуса и сокращение стоянок.
   Диалог с параметрами, уведомления и список применённых мер. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const TRIP_GAP_SEC = 300; // пауза в расписании длиннее 5 мин — конец рейса (отстой на конечной)

  const M = {
    list: [], // применённые меры: {id, kind, tr, title, text, at}
    seq: 0,
  };
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

  function apply() {
    const { kind, tr, state } = current;
    const route = App.net.routeByTr.get(tr);
    const line = route ? route.name : `ТС ${tr}`;
    let title, text;
    if (kind === 'reserve') {
      const { visits, k0, k1 } = state.plan;
      title = 'Выпущен дополнительный автобус';
      text = `Линия «${line}»: выход с «${stopName(visits, k0)}» в ${U.time(visits.plan[k0])}, до «${stopName(visits, k1)}»`;
    } else {
      const pr = state.params;
      title = 'Сокращены стоянки';
      text = `${pr.scope === 'route' ? `Вся линия «${line}»` : `ТС ${tr}`}: на остановках −${pr.cut_s} с` +
        (pr.short_layover ? ', отстой на конечной до 2 мин' : '');
    }
    const m = { id: ++M.seq, kind, tr, title, text, at: App.now };
    M.list.push(m);
    App.logEvent('info', `${title}. ${text}`);
    toast(`<b>${U.esc(title)}</b><br>${U.esc(text)}`, kind === 'reserve' ? 'ok' : 'info');
    $('measure-dlg').close();
    App.dirty = true;
  }

  /** Применённые меры по ТС — для карточек и панели ТС. */
  function forTr(tr) {
    return M.list.filter((m) => m.tr === tr);
  }

  function cancel(id) {
    const m = M.list.find((x) => x.id === id);
    if (!m) return;
    M.list = M.list.filter((x) => x.id !== id);
    App.logEvent('info', `Мера отменена: ${m.title.toLowerCase()} (${m.text})`);
    App.dirty = true;
  }

  function init(app) {
    App = app;
    $('m-apply').onclick = apply;
    $('m-cancel').onclick = () => $('measure-dlg').close();
  }

  Object.assign(M, { init, open, forTr, cancel, toast });
  window.Measures = M;
})();
