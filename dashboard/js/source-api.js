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
        this.last = { now, dataNow: now, down: false, vehicles, predictions, incidents, metrics };
        this.lastOk = Date.now() / 1000;
        this.error = null;
      } catch (e) {
        this.error = String(e.message || e);
      }
    }

    async pollVerified() {
      try {
        this.verified = (await this.get('/predictions/verified?limit=80')).map(normTimes);
      } catch (e) { /* эндпоинт необязателен */ }
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
