"""Mock-backend по контракту дашборда (CONTRACT.md) — эталон для настоящего backend.

Отдаёт те же эндпоинты, что будет отдавать FastAPI-backend, но данные берёт из
``data/replay.js`` и крутит виртуальные часы. Нужен, чтобы:

* проверить дашборд в режиме LIVE (``index.html?api=http://localhost:8000``);
* показать backend-разработчику точный формат ответов;
* отрепетировать деградацию: ``/demo/link?down=1`` — «обрыв потока NDTP».

Только стандартная библиотека Python::

    python dashboard/tools/mock_backend.py --port 8000 --speed 10
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent))
from build_fixtures import iso_msk, snapshot  # noqa: E402
from incident_rules import THRESHOLDS  # noqa: E402


class Clock:
    """Виртуальные часы воспроизведения с зацикливанием по дню."""

    def __init__(self, start: int, end: int, speed: float, begin: int):
        self.start, self.end, self.speed = start, end, speed
        self.t0_wall, self.t0 = time.time(), begin

    def now(self) -> int:
        t = self.t0 + (time.time() - self.t0_wall) * self.speed
        if t > self.end:
            self.t0_wall, self.t0 = time.time(), self.start
            t = self.start
        return int(t)


def load_replay(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    return json.loads(text[text.index("=") + 1:].rstrip().rstrip(";"))


def make_handler(replay: dict, clock: Clock, state: dict):
    meta = replay["meta"]
    network = {
        "stops": [{"stop_key": s[0], "lat": s[1], "lon": s[2], "name": s[3]} for s in replay["stops"]],
        "routes": replay["routes"],
    }
    cols = {k: i for i, k in enumerate(replay["predictions"]["columns"])}
    rows = replay["predictions"]["rows"]
    stop_names = {s[0]: s[3] for s in replay["stops"]}

    def live_snapshot():
        now = clock.now()
        if state["down_since"] is not None:
            snap = snapshot(replay, state["down_since"])
            snap["metrics"].update({"now": iso_msk(now), "ingest_status": "down", "source": "ndtp", "packets_per_min": 0})
            lag = now - state["down_since"]
            for p in snap["predictions"]:
                p["status"] = "stale"
                p["data_age_s"] = (p["data_age_s"] or 0) + lag
            for v in snap["vehicles"]:
                v["data_age_s"] += lag
                v["status"] = "live" if v["data_age_s"] <= 60 else ("stale" if v["data_age_s"] <= 300 else "offline")
            return now, snap
        snap = snapshot(replay, now)
        snap["metrics"].update({"ingest_status": "ok", "source": "replay"})
        return now, snap

    def verified(now: int, limit: int):
        c = cols
        out = []
        for r in rows:
            if r[c["outcome_at"]] is None or r[c["as_of"]] % 300:
                continue
            reveal = max(r[c["outcome_at"]], r[c["as_of"]])
            if reveal <= now:
                out.append((reveal, r))
        out.sort(key=lambda x: -x[0])
        res = []
        for _, r in out[:limit]:
            v = replay["visits"][str(r[c["tr_id"]])]
            k = v["id"].index(r[c["target_stop_id"]])
            res.append({
                "as_of": iso_msk(r[c["as_of"]]), "tr_id": r[c["tr_id"]],
                "target_time_begin": iso_msk(r[c["target_time_begin"]]),
                "target_stop_name": stop_names[v["stop"][k]],
                "prediction_delay_s": r[c["prediction_delay_s"]], "outcome_delay_s": r[c["outcome_delay_s"]],
                "cur_dev_s": r[c["cur_dev_s"]],
            })
        return res

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):  # тихий лог
            pass

        def send_json(self, obj, code=200):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):  # noqa: N802
            u = urlparse(self.path)
            q = parse_qs(u.query)
            path = u.path.rstrip("/") or "/"
            t0 = time.perf_counter()
            if path == "/health/live":
                return self.send_json({"status": "ok"})
            if path == "/health/ready":
                return self.send_json({"status": "ready", "model": meta["model"].get("version")})
            if path == "/network":
                return self.send_json(network)
            if path == "/config":
                return self.send_json({"thresholds": THRESHOLDS, "model": meta["model"], "horizon_s": meta["horizon_s"]})
            if path == "/schedule":
                tr = q.get("tr_id", [None])[0]
                v = replay["visits"].get(str(tr))
                if not v:
                    return self.send_json({"detail": "unknown tr_id"}, 404)
                now = clock.now()
                visits = [{"visit_id": v["id"][k], "stop_key": v["stop"][k], "time_plan": iso_msk(v["plan"][k]),
                           "time_fact": iso_msk(v["fact"][k]) if v["fact"][k] is not None and v["fact"][k] <= now else None}
                          for k in range(len(v["id"]))]
                return self.send_json({"tr_id": int(tr), "visits": visits})
            if path == "/demo/link":
                down = q.get("down", ["0"])[0] == "1"
                state["down_since"] = clock.now() if down else None
                return self.send_json({"ingest_status": "down" if down else "ok"})
            if path == "/predictions/verified":
                return self.send_json(verified(clock.now(), int(q.get("limit", ["80"])[0])))
            if path in ("/vehicles", "/predictions", "/incidents", "/metrics"):
                now, snap = live_snapshot()
                if path == "/metrics":
                    snap["metrics"]["request_ms"] = round((time.perf_counter() - t0) * 1000, 1)
                return self.send_json(snap[path.strip("/")])
            return self.send_json({"detail": "not found"}, 404)

    return Handler


def main():
    here = Path(__file__).resolve().parent
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--replay", default=str(here.parent / "data" / "replay.js"))
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--speed", type=float, default=1.0, help="скорость виртуальных часов (1 = реальное время)")
    args = ap.parse_args()
    replay = load_replay(Path(args.replay))
    m = replay["meta"]
    clock = Clock(m["day_start"], m["day_end"], args.speed, m["default_start"])
    state = {"down_since": None}
    srv = ThreadingHTTPServer((args.host, args.port), make_handler(replay, clock, state))
    print(f"mock-backend: http://{args.host}:{args.port}  (часы с {iso_msk(clock.now())}, x{args.speed})")
    srv.serve_forever()


if __name__ == "__main__":
    main()
