/* Экран «Журнал»: прогнозы с фактом и ошибкой, инциденты и события системы, выгрузка в CSV. */
(function () {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const LIMIT = 300;
  const st = { tab: 'predictions', rows: [], csv: null };

  /** Время по Москве в виде 2026-01-06 08:33:00 для CSV. */
  const stamp = (t) => (t == null ? '' : new Date((t + 10800) * 1000).toISOString().slice(0, 19).replace('T', ' '));

  function init(App) {
    const sel = $('jr-tr');
    for (const r of App.net.routes) {
      const o = document.createElement('option');
      o.value = r.tr_id;
      o.textContent = `ТС ${r.tr_id} · ${r.name}`;
      sel.appendChild(o);
    }
    const rerender = () => { App.lastPage = 0; App.dirty = true; };
    document.querySelectorAll('#jr-tabs button').forEach((b) => (b.onclick = () => {
      st.tab = b.dataset.jtab;
      document.querySelectorAll('#jr-tabs button').forEach((x) => x.classList.toggle('on', x === b));
      rerender();
    }));
    for (const id of ['jr-tr', 'jr-f-fact', 'jr-f-err', 'jr-f-alert']) $(id).onchange = rerender;
    $('jr-csv').onclick = () => { if (st.csv) download(`${st.tab}_${stamp(App.now).replace(/[: ]/g, '-')}.csv`, st.csv); };
    $('jr-table').addEventListener('click', (e) => {
      const row = e.target.closest('[data-open-tr]');
      if (row) App.openVehicle(+row.dataset.openTr);
    });
  }

  function download(name, rows) {
    const esc = (v) => {
      const s = v == null ? '' : String(v);
      return /[;"\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
    };
    // BOM и точка с запятой, чтобы Excel сразу понял кириллицу и колонки
    const csv = '﻿' + rows.map((r) => r.map(esc).join(';')).join('\r\n');
    const a = document.createElement('a');
    a.href = URL.createObjectURL(new Blob([csv], { type: 'text/csv;charset=utf-8' }));
    a.download = name;
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 2000);
  }

  function table(head, rows, total) {
    const more = total > rows.length ? `<div class="jr-more">Показаны последние ${rows.length} из ${U.num(total)} — остальное в CSV</div>` : '';
    return `<table class="tbl jr"><thead><tr>${head.map(([h, cls]) => `<th class="${cls || ''}">${h}</th>`).join('')}</tr></thead>` +
      `<tbody>${rows.join('') || `<tr><td colspan="${head.length}" class="empty">Записей нет</td></tr>`}</tbody></table>${more}`;
  }

  function renderPredictions(App, s) {
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const recs = App.src.predictionRecords(now, dataNow);
    const tr = $('jr-tr').value, onlyFact = $('jr-f-fact').checked, bigErr = $('jr-f-err').checked, onlyAlert = $('jr-f-alert').checked;
    const known = (r) => r.outcome != null && r.outcome_at != null && r.outcome_at <= now;
    let rows = recs.filter((r) => (!tr || String(r.tr_id) === tr)
      && (!onlyFact || known(r))
      && (!bigErr || (known(r) && Math.abs(r.pred - r.outcome) > 120))
      && (!onlyAlert || r.severity === 'critical' || r.severity === 'warning' || r.severity === 'early'));
    rows = rows.slice().reverse();
    $('jr-n-pred').textContent = U.num(recs.length);
    $('jr-count').textContent = `${U.num(rows.length)} прогнозов`;
    const status = (r) => (r.status === 'fallback' ? 'упрощённый' : r.status === 'stale' ? 'устаревший' : 'модель');
    st.csv = [['выпущен', 'ТС', 'цель', 'план', 'прогноз_с', 'факт_с', 'ошибка_с', 'уровень', 'причина', 'вероятность_опоздания', 'прогноз_от']]
      .concat(rows.map((r) => {
        const k = known(r);
        return [stamp(r.as_of), r.tr_id, r.target_stop_name, stamp(r.target_time_begin), r.pred, k ? r.outcome : '',
          k ? Math.round(Math.abs(r.pred - r.outcome)) : '', U.SEV[r.severity].label, r.reason_title || '',
          r.late_probability != null ? r.late_probability : '', status(r)];
      }));
    const html = rows.slice(0, LIMIT).map((r) => {
      const k = known(r), e = k ? Math.abs(r.pred - r.outcome) : null;
      return `<tr class="${e != null && e > 120 ? 'flag' : ''}" data-open-tr="${r.tr_id}">` +
        `<td>${U.time(r.as_of)}</td><td>${r.tr_id}</td><td class="jr-name" title="${U.esc(r.target_stop_name)}">${U.esc(r.target_stop_name)}</td>` +
        `<td>${U.time(r.target_time_begin)}</td><td class="r">${U.delay(r.pred)}</td>` +
        `<td class="r">${k ? U.delay(r.outcome) : `<span class="muted">${r.outcome_at ? 'ждём' : '—'}</span>`}</td>` +
        `<td class="r">${e != null ? U.dur(e) : ''}</td><td>${U.sevBadge(r.severity, U.SEV[r.severity].short)}</td>` +
        `<td class="jr-name">${U.esc(r.reason_title || '')}</td><td class="muted">${status(r)}</td></tr>`;
    });
    return table([['Выпущен'], ['ТС'], ['Цель'], ['План'], ['Прогноз', 'r'], ['Факт', 'r'], ['Ошибка', 'r'], ['Уровень'], ['Причина'], ['Прогноз от']],
      html, rows.length);
  }

  function renderIncidents(App, s) {
    const now = App.now, dataNow = s.dataNow != null ? s.dataNow : now;
    const tr = $('jr-tr').value;
    const all = App.src.incidentRecords(now, dataNow);
    const rows = all.filter((i) => !tr || String(i.tr_id) === tr).sort((a, b) => b.opened_at - a.opened_at);
    $('jr-n-inc').textContent = U.num(all.length);
    $('jr-count').textContent = `${U.num(rows.length)} инцидентов`;
    const lead = (i) => (i.fact_at != null ? (i.fact_at - i.opened_at) / 60 : null);
    st.csv = [['обнаружен', 'закрыт', 'ТС', 'тип', 'макс_уровень', 'причина', 'цель', 'прогноз_с', 'факт_с', 'предупреждение_за_мин']]
      .concat(rows.map((i) => [stamp(i.opened_at), stamp(i.closed_at), i.tr_id, i.kind === 'early' ? 'раньше графика' : 'опоздание',
        U.SEV[i.peak] ? U.SEV[i.peak].label : '', i.reason_title || '', i.alert_target_name || '', i.alert_pred,
        i.outcome != null ? i.outcome : '', lead(i) != null ? Math.round(lead(i)) : '']));
    const html = rows.slice(0, LIMIT).map((i) => {
      const open = i.closed_at == null;
      const dur = (open ? now : i.closed_at) - i.opened_at;
      const e = i.outcome != null ? Math.abs(i.alert_pred - i.outcome) : null;
      const ld = lead(i);
      return `<tr data-open-tr="${i.tr_id}"><td>${U.time(i.opened_at)}</td>` +
        `<td>${open ? '<b>активен</b>' : U.time(i.closed_at)}</td><td class="r">${U.dur(dur)}</td><td>${i.tr_id}</td>` +
        `<td>${U.sevBadge(i.peak, i.kind === 'early' ? 'раньше графика' : U.SEV[i.peak] ? U.SEV[i.peak].short : '')}</td>` +
        `<td class="jr-name">${U.esc(i.reason_title || '')}</td><td class="jr-name" title="${U.esc(i.alert_target_name || '')}">${U.esc(i.alert_target_name || '')}</td>` +
        `<td class="r">${U.delay(i.alert_pred)}</td><td class="r">${i.outcome != null ? U.delay(i.outcome) : '<span class="muted">—</span>'}</td>` +
        `<td class="r">${e != null ? U.dur(e) : ''}</td><td class="r">${ld != null ? `${Math.round(ld)} мин` : ''}</td></tr>`;
    });
    return table([['Обнаружен'], ['Закрыт'], ['Длилось', 'r'], ['ТС'], ['Уровень'], ['Причина'], ['Цель'], ['Прогноз', 'r'], ['Факт', 'r'], ['Ошибка', 'r'], ['Заранее', 'r']],
      html, rows.length);
  }

  function renderEvents(App) {
    const ev = App.events;
    $('jr-n-ev').textContent = U.num(ev.length);
    $('jr-count').textContent = `${U.num(ev.length)} событий с открытия страницы`;
    st.csv = [['время', 'уровень', 'событие']].concat(ev.map((e) => [stamp(e.t), e.kind, e.text]));
    const icon = (k) => (U.SEV[k] ? U.sevIcon(k, 13) : k === 'info' ? '<span class="ev-dot"></span>' : U.sevIcon('unknown', 13));
    const html = ev.slice(0, LIMIT).map((e) => {
      const m = /ТС (\d+)/.exec(e.text);
      return `<tr${m ? ` data-open-tr="${m[1]}"` : ''}><td>${U.time(e.t, true)}</td><td class="jr-ev">${icon(e.kind)}${U.esc(e.text)}</td></tr>`;
    });
    return table([['Время'], ['Событие']], html, ev.length);
  }

  function render(App, s) {
    document.querySelectorAll('.jr-f-pred').forEach((el) => (el.hidden = st.tab !== 'predictions'));
    $('jr-tr').parentElement.hidden = st.tab === 'events';
    const html = st.tab === 'predictions' ? renderPredictions(App, s)
      : st.tab === 'incidents' ? renderIncidents(App, s) : renderEvents(App);
    if (st.tab !== 'predictions') $('jr-n-pred').textContent = U.num(App.src.predictionRecords(App.now, s.dataNow).length);
    if (st.tab !== 'incidents') $('jr-n-inc').textContent = U.num(App.src.incidentRecords(App.now, s.dataNow).length);
    if (st.tab !== 'events') $('jr-n-ev').textContent = U.num(App.events.length);
    if (html !== App.html.journal) {
      const box = $('jr-table'), y = box.scrollTop;
      App.html.journal = html;
      box.innerHTML = html;
      box.scrollTop = y;
    }
  }

  window.Views = window.Views || {};
  window.Views.journal = { init, render };
})();
