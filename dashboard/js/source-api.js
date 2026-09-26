/* Источник данных LIVE: опрос backend по контракту (CONTRACT.md).
   Включается параметром ?api=http://localhost:8000. При ошибках связи держит
   последнее полученное состояние и сообщает об этом (деградация без падения). */
(function () {
  'use strict';

  const TS_FIELDS = ['as_of', 'generated_at', 'target_time_begin', 'predicted_arrival', 'first_detected_at',
    'updated_at', 'closed_at', 'event_time', 'now', 'last_packet_at', 'alert_target_time_begin'];

  function normTimes(o) {
    if (!o || typeof o !== 'object') return o;
    for (const k of TS_FIELDS) if (k in o) o[k] = U.ts(o[k]);
    return o;
  }

  class ApiSource {
    constructor(base, pollMs) {
      this.kind = 'api';
      this.base = base.replace(/\/$/, '');
      this.pollMs = pollMs || 2000;
      this.meta = { thresholds: { early: -60, warning: 60, critical: 120 }, grid_step_s: 60, model: {} };
      this.thr = this.meta.thresholds;
      this.last = null;
      this.lastOk = null;
      this.error = null;
      this.verified = [];
      this.speedBuf = new Map(); // tr -> [[t, speed]]
      this.fcBuf = new Map(); // tr -> Map(target_stop_id -> [target_time, pred, as_of])
      this.sched = new Map(); // tr -> {data, fetchedAt}
      // История для «Аналитики» и «Журнала»: копится с момента открытия страницы,
      // сверенные с фактом прогнозы backend отдаёт целиком (/predictions/verified?all=true).
      this.predLog = new Map(); // sample_id -> запись
      this.incLog = new Map(); // incident_id -> запись
      this.hist = { t: [], critical: [], warning: [], early: [], ok: [], packets: [], latency: [] };
    }

    async get(path) {
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), 5000);
      try {
        const r = await fetch(this.base + path, { cache: 'no-store', signal: ctl.signal });
        if (!r.ok) throw new Error(`${path}: HTTP ${r.status}`);
        return await r.json();
      } finally {
        clearTimeout(timer);
      }
    }

    async init() {
      // Сеть нужна для карты — без неё стартовать нельзя, повторяем до успеха.
      for (let attempt = 0; ; attempt++) {
        try {
          this.network = new Network(await this.get('/network'));
          break;
        } catch (e) {
          this.error = String(e.message || e);
          if (attempt >= 2) throw new Error(`${this.base}: ${this.error}`);
          await new Promise((res) => setTimeout(res, 1500));
        }
      }
      try {
        const cfg = await this.get('/config');
        if (cfg.thresholds) this.meta.thresholds = this.thr = cfg.thresholds;
        Object.assign(this.meta.model, cfg.model || {});
        if (cfg.replay) {
          // Backend воспроизводит исторический день — дашборд может им управлять.
          this.replayInfo = cfg.replay;
          this.meta.day_start = U.ts(cfg.replay.day_start);
          this.meta.day_end = U.ts(cfg.replay.day_end);
        }
      } catch (e) { /* /config необязателен */ }
      await this.poll();
      setInterval(() => this.poll(), this.pollMs);
      setInterval(() => this.pollVerified(), 10000);
      this.pollVerified();
    }

    async poll() {
      try {
        const [veh, preds, incs, met] = await Promise.all(
          ['/vehicles', '/predictions', '/incidents', '/metrics'].map((p) => this.get(p)));
        const metrics = normTimes(met || {});
        const now = metrics.now || Date.now() / 1000;
        const predictions = new Map();
        for (const p of preds) {
          normTimes(p);
          if (p.severity == null) p.severity = U.severityOf(p.prediction_delay_s, this.thr);
          predictions.set(p.tr_id, p);
          let fb = this.fcBuf.get(p.tr_id);
          if (!fb) this.fcBuf.set(p.tr_id, (fb = new Map()));
          fb.set(p.target_stop_id, [p.target_time_begin, p.prediction_delay_s, p.as_of]);
        }
        const vehicles = veh.map((v) => {
          normTimes(v);
          if (v.data_age_s == null) v.data_age_s = now - v.event_time;
          if (!v.status) v.status = v.data_age_s <= 60 ? 'live' : v.data_age_s <= 300 ? 'stale' : 'offline';
          const p = predictions.get(v.tr_id);
          v.severity = p ? p.severity : 'unknown';
          v.in_service = !!p;
          let sb = this.speedBuf.get(v.tr_id);
          if (!sb) this.speedBuf.set(v.tr_id, (sb = []));
          if (!sb.length || sb[sb.length - 1][0] !== v.event_time) sb.push([v.event_time, v.speed]);
          while (sb.length && sb[0][0] < now - 3600) sb.shift();
          return v;
        });
        for (const [tr, fb] of this.fcBuf) for (const [k, x] of fb) if (x[2] < now - 5400) fb.delete(k);
        const incidents = incs.map((i) => {
          normTimes(i);
          if (!i.prediction_status) i.prediction_status = (predictions.get(i.tr_id) || {}).status;
          return i;
        });
        this.remember(now, predictions, incidents, metrics);
        this.last = { now, dataNow: now, down: false, vehicles, predictions, incidents, metrics };
        this.lastOk = Date.now() / 1000;
        this.error = null;
      } catch (e) {
        this.error = String(e.message || e);
      }
    }

    async pollVerified() {
      try {
        const all = (await this.get('/predictions/verified?all=true&limit=20000')).map(normTimes);
        this.verified = all.slice(0, 80);
        for (const v of all) {
          const key = v.sample_id || `${v.tr_id}_${v.as_of}`;
          const r = this.predLog.get(key) || this.toRec(v, key);
          r.outcome = v.outcome_delay_s;
          r.outcome_at = v.target_time_begin + v.outcome_delay_s;
          this.predLog.set(key, r);
        }
      } catch (e) { /* эндпоинт необязателен */ }
    }

    toRec(p, key) {
      const pred = p.prediction_delay_s;
      return {
        sample_id: key, as_of: p.as_of, tr_id: p.tr_id, target_stop_name: p.target_stop_name,
        target_time_begin: p.target_time_begin, pred, late_probability: p.late_probability,
        cur_dev: p.cur_dev_s, status: p.status, severity: p.severity || U.severityOf(pred, this.thr),
        reason_title: p.reason ? p.reason.title : p.reason_title || null, outcome: null, outcome_at: null,
      };
    }

    /** Запомнить прогнозы, инциденты и метрики для истории. */
    remember(now, predictions, incidents, metrics) {
      for (const p of predictions.values()) {
        const key = p.sample_id || `${p.tr_id}_${p.as_of}`;
        if (!this.predLog.has(key)) this.predLog.set(key, this.toRec(p, key));
      }
      for (const i of incidents) {
        const prev = this.incLog.get(i.incident_id) || {};
        this.incLog.set(i.incident_id, {
          incident_id: i.incident_id, tr_id: i.tr_id, kind: i.kind, opened_at: i.first_detected_at,
          closed_at: i.status === 'resolved' ? i.closed_at || i.updated_at : null,
          peak: i.peak_severity, reason_title: i.suspected_reason ? i.suspected_reason.title : prev.reason_title,
          alert_pred: i.alert_prediction_delay_s != null ? i.alert_prediction_delay_s : i.prediction_delay_s,
          alert_target_name: i.alert_target_stop_name || i.target_stop_name,
          alert_target_time: i.alert_target_time_begin || i.target_time_begin,
          outcome: i.outcome_delay_s, fact_at: i.outcome_delay_s != null && i.alert_target_time_begin
            ? i.alert_target_time_begin + i.outcome_delay_s : null,
        });
      }
      const h = this.hist;
      if (h.t.length && now - h.t[h.t.length - 1] < 30) return; // точка не чаще раза в 30 с
      const c = { critical: 0, warning: 0, early: 0, ok: 0 };
      for (const p of predictions.values()) if (c[p.severity] != null) c[p.severity]++;
      h.t.push(now);
      for (const k of Object.keys(c)) h[k].push(c[k]);
      h.packets.push(metrics.packets_per_min != null ? metrics.packets_per_min : null);
      h.latency.push(metrics.inference_latency_ms_p95 != null ? metrics.inference_latency_ms_p95 : null);
      if (h.t.length > 5000) for (const k of Object.keys(h)) h[k].shift();
    }

    predictionRecords() {
      return [...this.predLog.values()].sort((a, b) => a.as_of - b.as_of);
    }

    incidentRecords() {
      return [...this.incLog.values()];
    }

    dayHistory() {
      return this.hist;
    }

    clock() {
      if (!this.last) return Date.now() / 1000;
      // Часы backend + время с последнего успешного ответа (с учётом скорости воспроизведения).
      const m = this.last.metrics;
      const speed = m.replay_speed != null ? m.replay_speed : 1;
      return m.now ? m.now + (Date.now() / 1000 - this.lastOk) * speed : Date.now() / 1000;
    }

    /** Управление воспроизведением в backend: start | speed | stop | link. */
    async control(action, params) {
      const q = new URLSearchParams(params || {}).toString();
      const r = await fetch(`${this.base}/demo/${action}?${q}`, { method: 'POST' });
      if (!r.ok) throw new Error(`/demo/${action}: HTTP ${r.status}`);
      await this.poll();
      return r.json();
    }

    snapshot() {
      const base = this.last || { now: Date.now() / 1000, vehicles: [], predictions: new Map(), incidents: [], metrics: {} };
      const lost = this.error && this.lastOk && Date.now() / 1000 - this.lastOk > 5;
      return {
        ...base,
        down: !!lost || base.metrics.ingest_status === 'down',
        backendDown: !!lost,
        backendError: this.error,
        lastOk: this.lastOk,
        verified: this.verified,
      };
    }

    schedule(tr) {
      const c = this.sched.get(tr);
      if (!c || Date.now() - c.fetchedAt > 30000) {
        if (!c) this.sched.set(tr, { data: null, fetchedAt: Date.now() });
        else c.fetchedAt = Date.now();
        this.get(`/schedule?tr_id=${encodeURIComponent(tr)}`).then((s) => {
          const vs = s.visits || [];
          const data = {
            id: vs.map((x) => x.visit_id), stop: vs.map((x) => x.stop_key),
            plan: vs.map((x) => U.ts(x.time_plan)), fact: vs.map((x) => U.ts(x.time_fact)),
          };
          data.pos = new Map(data.id.map((id, k) => [id, k]));
          this.sched.set(tr, { data, fetchedAt: Date.now() });
        }).catch(() => {});
      }
      return c ? c.data : null;
    }

    history(tr, now) {
      const out = { facts: [], forecasts: [], speed: (this.speedBuf.get(tr) || []).slice() };
      const v = this.schedule(tr);
      if (v) {
        for (let k = 0; k < v.plan.length; k++) {
          if (v.plan[k] >= now - 5400 && v.fact[k] != null && v.fact[k] <= now) out.facts.push([v.plan[k], v.fact[k] - v.plan[k]]);
        }
      }
      const fb = this.fcBuf.get(tr);
      if (fb) out.forecasts = [...fb.values()].sort((a, b) => a[0] - b[0]);
      return out;
    }
  }

  window.ApiSource = ApiSource;
})();
