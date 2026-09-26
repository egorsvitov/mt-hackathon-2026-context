/* Источник данных REPLAY: воспроизведение дня из data/replay.js (финализируется map_matching).
   Отдаёт те же объекты, что backend по контракту (CONTRACT.md), только времена — epoch-секунды.
   Всё, что показывается на момент now, вычисляется только из данных с временем <= now. */
(function () {
  'use strict';

  const ALERT = { warning: 1, critical: 1, early: 1 };

  class ReplaySource {
    constructor(data) {
      this.kind = 'replay';
      const m = (this.meta = data.meta);
      this.thr = m.thresholds;
      this.network = new Network({
        stops: data.stops.map((s) => ({ stop_key: s[0], lat: s[1], lon: s[2], name: s[3] })),
        routes: data.routes,
      });

      this.visits = new Map();
      for (const [tr, v] of Object.entries(data.visits)) {
        v.pos = new Map(v.id.map((id, k) => [id, k]));
        this.visits.set(+tr, v);
      }

      this.tel = new Map();
      for (const [tr, v] of Object.entries(data.telemetry)) {
        const mm = v.mm_matched || null;
        const lastMm = new Int32Array(v.t.length);
        let last = -1;
        for (let k = 0; k < v.t.length; k++) {
          if (!mm || mm[k]) last = k;
          lastMm[k] = last;
        }
        this.tel.set(+tr, {
          unit_id: v.unit_id, t: Float64Array.from(v.t, (x) => x + m.t0),
          lat: v.lat, lon: v.lon, spd: v.spd, hdg: v.hdg, mm, lastMm,
          pattern: v.mm_route_pattern_id || null,
        });
      }

      const c = (this.c = Object.fromEntries(data.predictions.columns.map((k, i) => [k, i])));
      const rows = (this.rows = data.predictions.rows);
      this.byTr = new Map();
      rows.forEach((r, i) => {
        const tr = r[c.tr_id];
        let b = this.byTr.get(tr);
        if (!b) this.byTr.set(tr, (b = { idx: [], asof: [] }));
        b.idx.push(i);
        b.asof.push(r[c.as_of]);
      });

      // Проверка прогнозов: исход становится известен, когда наступил факт прибытия.
      const rev = [];
      rows.forEach((r, i) => {
        if (r[c.outcome_at] == null || r[c.outcome_delay_s] == null) return;
        rev.push([Math.max(r[c.outcome_at], r[c.as_of]), i]);
      });
      rev.sort((a, b) => a[0] - b[0]);
      this.revT = rev.map((x) => x[0]);
      this.cumErr = [0];
      this.cumBase = [0];
      for (const [, i] of rev) {
        const r = rows[i];
        this.cumErr.push(this.cumErr[this.cumErr.length - 1] + Math.abs(r[c.prediction_delay_s] - r[c.outcome_delay_s]));
        this.cumBase.push(this.cumBase[this.cumBase.length - 1] + Math.abs(r[c.cur_dev_s] - r[c.outcome_delay_s]));
      }
      // Журнал проверки — на 5-минутной сетке, как у организаторов.
      const ver = rev.filter(([, i]) => rows[i][c.as_of] % 300 === 0);
      this.verT = ver.map((x) => x[0]);
      this.verI = ver.map((x) => x[1]);

      this.incidents = data.incidents.map((inc) => ({ ...inc, asof: inc.preds.map((i) => rows[i][c.as_of]) }));
    }

    clock() { return this.meta.default_start; }

    schedule(tr) { return this.visits.get(tr) || null; }

    /** Строка прогноза -> объект Prediction контракта. */
    pred(i, now) {
      const c = this.c, r = this.rows[i], m = this.meta;
      const tr = r[c.tr_id];
      const v = this.visits.get(tr);
      const net = this.network;
      const pred = r[c.prediction_delay_s];
      const sev = U.severityOf(pred, this.thr);
      const feats = {
        speed_5m_kmh: r[c.speed_5m_kmh], speed_norm_kmh: r[c.speed_norm_kmh], stationary_s: r[c.stationary_s],
        dev_trend_15m_s: r[c.dev_trend_15m_s], cur_dev_s: r[c.cur_dev_s], required_speed_kmh: r[c.required_speed_kmh],
        gps_age_s: r[c.gps_age_s],
      };
      const code = r[c.reason];
      const flags = new Set(code && m.reasons[code] ? m.reasons[code].flags : []);
      const evidence = m.evidence_spec.map((s) => ({
        code: s.code, label: s.label, unit: s.unit, value: feats[s.feature],
        norm: s.norm_feature ? feats[s.norm_feature] : null, flag: flags.has(s.code),
      }));
      const tgt = r[c.target_stop_id], from = r[c.from_stop_id];
      const outcomeKnown = r[c.outcome_at] != null && r[c.outcome_at] <= now;
      return {
        sample_id: `${tr}_${r[c.as_of] + 10800}`,
        tr_id: tr,
        route_id: `R${tr}`,
        as_of: r[c.as_of],
        generated_at: r[c.as_of],
        target_stop_id: tgt,
        target_stop_name: net.stopName(v.stop[v.pos.get(tgt)]),
        target_time_begin: r[c.target_time_begin],
        horizon_s: r[c.target_time_begin] - r[c.as_of],
        prediction_delay_s: pred,
        predicted_arrival: r[c.target_time_begin] + pred,
        late_probability: r[c.late_probability],
        cur_dev_s: r[c.cur_dev_s],
        severity: sev,
        status: r[c.status],
        model_version: m.model.version,
        data_age_s: r[c.gps_age_s],
        segment: {
          from_stop_id: from, from_stop_name: net.stopName(v.stop[v.pos.get(from)]),
          to_stop_id: tgt, to_stop_name: net.stopName(v.stop[v.pos.get(tgt)]),
        },
        reason: code ? { code, title: m.reasons[code].title, detail: r[c.detail] } : null,
        evidence,
        recommendation: code ? m.reasons[code].recommendation : null,
        outcome_delay_s: outcomeKnown ? r[c.outcome_delay_s] : null,
      };
    }

    /** Состояние на now. linkDownSince — момент имитированного обрыва связи (или null). */
    snapshot(now, linkDownSince) {
      const down = linkDownSince != null && now > linkDownSince;
      const dataNow = down ? linkDownSince : now;
      const step = this.meta.grid_step_s;
      const c = this.c, rows = this.rows;

      const predictions = new Map();
      for (const [tr, b] of this.byTr) {
        const k = U.countLE(b.asof, dataNow) - 1;
        if (k < 0 || b.asof[k] <= dataNow - 3 * step) continue;
        const p = this.pred(b.idx[k], now);
        if (down) {
          p.status = 'stale';
          p.data_age_s = (p.data_age_s || 0) + (now - dataNow);
        }
        predictions.set(tr, p);
      }

      const vehicles = [];
      let packets = 0, lastPacket = null;
      for (const [tr, T] of this.tel) {
        const k = U.countLE(T.t, dataNow) - 1;
        if (k < 0) continue;
        packets += k + 1 - U.countLE(T.t, dataNow - 60);
        lastPacket = Math.max(lastPacket || 0, T.t[k]);
        const route = this.network.routeByTr.get(tr);
        if (!route) continue; // hide telemetry without a planned/catalogued route
        let pos = k;
        if (T.mm && !T.mm[k]) {
          pos = T.lastMm[k];
          if (pos < 0 || T.t[k] - T.t[pos] > 180) continue;
        }
        const age = now - T.t[pos];
        if (age > 3600) continue;
        const p = predictions.get(tr);
        vehicles.push({
          tr_id: tr, unit_id: T.unit_id, route_id: route.route_id,
          event_time: T.t[pos], lat: T.lat[pos] / 1e5, lon: T.lon[pos] / 1e5, speed: T.spd[pos], heading: T.hdg[pos],
          location_valid: true, data_age_s: age, position_quality: pos === k ? 'matched' : 'stale_match',
          route_pattern_id: T.pattern ? T.pattern[pos] : null,
          status: age <= 60 ? 'live' : age <= 300 ? 'stale' : 'offline',
          severity: p ? p.severity : 'unknown', in_service: !!p, source: 'replay',
        });
      }

      const incidents = [];
      for (const inc of this.incidents) {
        if (inc.opened_at > dataNow) continue;
        const closed = inc.closed_at != null && inc.closed_at <= dataNow;
        if (closed && inc.closed_at <= now - 900) continue;
        const k = U.countLE(inc.asof, dataNow) - 1;
        if (k < 0) continue;
        let headI = null, peak = 'ok';
        for (let q = 0; q <= k; q++) {
          const i = inc.preds[q];
          const s = U.severityOf(rows[i][c.prediction_delay_s], this.thr);
          if (ALERT[s]) {
            headI = i;
            if (U.SEV[s].rank > U.SEV[peak].rank) peak = s;
          }
        }
        const cur = this.pred(inc.preds[k], now);
        const head = headI != null ? this.pred(headI, now) : cur;
        incidents.push({
          incident_id: inc.incident_id, tr_id: inc.tr_id, route_id: `R${inc.tr_id}`, kind: inc.kind,
          status: closed ? 'resolved' : 'active',
          first_detected_at: inc.opened_at, updated_at: cur.as_of, closed_at: closed ? inc.closed_at : null,
          severity: closed ? 'ok' : cur.severity, peak_severity: peak,
          target_stop_id: cur.target_stop_id, target_stop_name: cur.target_stop_name,
          target_time_begin: cur.target_time_begin, horizon_s: cur.horizon_s,
          prediction_delay_s: cur.prediction_delay_s, predicted_arrival: cur.predicted_arrival,
          late_probability: cur.late_probability, segment: cur.segment, prediction_status: down ? 'stale' : cur.status,
          suspected_reason: head.reason, evidence: head.evidence, recommendation: head.recommendation,
          alert_prediction_delay_s: head.prediction_delay_s, alert_target_stop_name: head.target_stop_name,
          alert_target_time_begin: head.target_time_begin, outcome_delay_s: head.outcome_delay_s,
        });
      }

      const n = U.countLE(this.revT, now);
      const nv = U.countLE(this.verT, now);
      const verified = [];
      for (let q = nv - 1; q >= Math.max(0, nv - 80); q--) {
        const r = rows[this.verI[q]];
        const tr = r[c.tr_id], v = this.visits.get(tr);
        verified.push({
          as_of: r[c.as_of], tr_id: tr, target_time_begin: r[c.target_time_begin],
          target_stop_name: this.network.stopName(v.stop[v.pos.get(r[c.target_stop_id])]),
          prediction_delay_s: r[c.prediction_delay_s], outcome_delay_s: r[c.outcome_delay_s],
          cur_dev_s: r[c.cur_dev_s],
        });
      }

      const mm = this.meta.model;
      const metrics = {
        now, mode: 'replay', source: down ? 'down' : 'replay', ingest_status: down ? 'down' : 'ok',
        model_version: mm.version, vehicles_live: vehicles.filter((v) => v.status === 'live').length,
        packets_per_min: down ? 0 : packets, last_packet_at: lastPacket,
        inference_latency_ms_p50: mm.latency_ms_single, inference_latency_ms_p95: mm.latency_ms_single,
        queue_lag_s: 0, reconnects: 0,
        mae_live_s: n ? this.cumErr[n] / n : null, mae_baseline_live_s: n ? this.cumBase[n] / n : null,
        n_verified: n, n_verified_grid5: nv,
        offline_eval: mm.mae_model != null ? { mae_model: mm.mae_model, mae_cur_dev: mm.mae_cur_dev, n: mm.n_eval, on: mm.eval_on } : null,
      };
      return { now, dataNow, down, vehicles, predictions, incidents, verified, metrics };
    }

    /** Ряды для графиков выбранного ТС. */
    history(tr, now, dataNow) {
      const since = now - 5400;
      const out = { facts: [], forecasts: [], speed: [] };
      const v = this.visits.get(tr);
      if (v) {
        for (let k = U.countLE(v.plan, since); k < v.plan.length && v.plan[k] <= now + 1800; k++) {
          if (v.fact[k] != null && v.fact[k] <= dataNow) out.facts.push([v.plan[k], v.fact[k] - v.plan[k]]);
        }
      }
      const b = this.byTr.get(tr);
      if (b) {
        const c = this.c;
        const last = new Map();
        for (let q = U.countLE(b.asof, since); q < b.asof.length && b.asof[q] <= dataNow; q++) {
          const r = this.rows[b.idx[q]];
          last.set(r[c.target_stop_id], [r[c.target_time_begin], r[c.prediction_delay_s], r[c.as_of]]);
        }
        out.forecasts = [...last.values()].sort((a, b2) => a[0] - b2[0]);
      }
      const T = this.tel.get(tr);
      if (T) {
        for (let k = U.countLE(T.t, now - 3600); k < T.t.length && T.t[k] <= dataNow; k++) out.speed.push([T.t[k], T.spd[k]]);
      }
      return out;
    }
  }

  window.ReplaySource = ReplaySource;
})();
