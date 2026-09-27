"""Тесты надёжности системы (критерий 5): запускаются против поднятого docker compose.

    python tests/reliability/make_multibus.py --data-dir ../dataset --out ../synthetic/multibus
    python tests/reliability/run_tests.py                 # все сценарии
    python tests/reliability/run_tests.py ml_outage soak   # выбранные

Каждый сценарий пишет results/<имя>.json; сводка — в конце прогона. Сценарии пересоздают
backend с нужным окружением, в конце backend возвращается в режим по умолчанию.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import socket
import subprocess
import threading
import time
from pathlib import Path

import aiohttp
import pandas as pd
import requests

import harness as H

RESULTS = Path(__file__).with_name("results")
README_UNITS = [893159, 913870, 786201]  # пример конфигурации эмулятора из ndtp-parser/README.md


def result(name: str, verdict: str, summary: str, **data) -> dict:
    return {"scenario": name, "verdict": verdict, "summary": summary, **data}


def replay_now() -> float:
    return H.epoch(H.get("/metrics")["now"])


def snap(trs) -> dict:
    """Время последней отметки и последнего прогноза по каждому ТС."""
    vs = {str(v["tr_id"]): v for v in H.get("/vehicles")}
    ps = {str(p["tr_id"]): p for p in H.get("/predictions")}
    return {str(tr): {"event_time": vs.get(str(tr), {}).get("event_time"),
                      "as_of": ps.get(str(tr), {}).get("as_of")} for tr in trs}


def advanced(before: dict, after: dict, key: str) -> dict[str, bool]:
    return {tr: (H.epoch(after[tr][key]) or 0) > (H.epoch(before[tr][key]) or 0) for tr in before}


# ============================================================================ 1. холодный старт

def s_cold_start() -> dict:
    H.compose("down")
    t0 = time.monotonic()
    H.compose("up", "-d")
    marks: dict[str, float] = {}
    checks = {
        "backend_live": lambda: requests.get(f"{H.BACKEND}/health/live", timeout=1).ok,
        "backend_ready": lambda: requests.get(f"{H.BACKEND}/health/ready", timeout=1).ok,
        "dashboard": lambda: requests.get(H.DASHBOARD, timeout=1).ok,
        "ndtp_port": lambda: socket.create_connection(H.NDTP_ADDR, timeout=1).close() is None,
        "first_prediction": lambda: len(H.get("/predictions", timeout=2)) > 0,
    }
    while time.monotonic() - t0 < 300 and len(marks) < len(checks):
        for k, f in checks.items():
            if k in marks:
                continue
            try:
                if f():
                    marks[k] = round(time.monotonic() - t0, 1)
            except Exception:  # noqa: BLE001
                pass
        time.sleep(0.5)
    miss = [k for k in checks if k not in marks]
    return result("cold_start", "fail" if miss else "ok",
                  f"все сервисы готовы за {max(marks.values())} с" if not miss else f"не дождались: {miss}",
                  seconds=marks)


# ============================================================================ 2. мусор в API телеметрии

def s_api_garbage() -> dict:
    H.backend_up()
    time.sleep(5)
    now = int(replay_now())
    base = {"tr_id": "122048", "timestamp": now, "lat": 55.7, "lon": 37.6, "speed": 20, "heading": 90, "location_valid": True}
    js = {"Content-Type": "application/json"}
    cases = [
        ("пустой JSON", {"json": {}}, "4xx"),
        ("не JSON", {"data": "garbage", "headers": js}, "4xx"),
        ("timestamp строкой", {"json": {**base, "timestamp": "abc"}}, "4xx"),
        ("timestamp дробный", {"json": {**base, "timestamp": now + 0.5}}, "4xx"),
        ("tr_id числом", {"json": {**base, "tr_id": 122048}}, "any"),
        ("lat строкой", {"json": {**base, "lat": "55.7"}}, "2xx"),
        ("координаты вне Земли", {"json": {**base, "tr_id": "REL_X1", "lat": 999, "lon": -999}}, "2xx"),
        ("скорость −50, курс 720", {"json": {**base, "tr_id": "REL_X2", "speed": -50, "heading": 720}}, "2xx"),
        ("tr_id 100 000 символов", {"json": {**base, "tr_id": "X" * 100_000}}, "2xx"),
        ("timestamp 1970", {"json": {**base, "tr_id": "REL_X3", "timestamp": 0}}, "2xx"),
        ("дубль пакета", {"json": {**base, "tr_id": "REL_X4"}}, "2xx"),
        ("дубль пакета, повтор", {"json": {**base, "tr_id": "REL_X4"}}, "2xx"),
        ("невалидные координаты без lat/lon", {"json": {**base, "lat": None, "lon": None, "location_valid": False}}, "2xx"),
        ("тело 1 МБ", {"json": {**base, "tr_id": "REL_X5", "pad": "x" * 1_000_000}}, "any"),
        # NaN — не JSON по стандарту, но json.loads его принимает. ТС с расписанием и новое окно T,
        # чтобы NaN дошёл до признаков, модели и ответов API.
        ("NaN в координатах", {"data": json.dumps({**base, "timestamp": now + 31, "lat": float("nan"), "lon": float("nan")}), "headers": js}, "any"),
        ("Infinity в скорости", {"data": json.dumps({**base, "tr_id": "122613", "timestamp": now + 31, "speed": float("inf")}), "headers": js}, "any"),
    ]
    rows, bad = [], []
    for name, kw, expect in cases:
        try:
            code = H.post("/stream/telemetry", **kw).status_code
        except requests.RequestException as e:
            code = type(e).__name__
        ok = (expect == "any" and isinstance(code, int) and code < 500) or \
             (expect == "2xx" and code == 200) or (expect == "4xx" and isinstance(code, int) and 400 <= code < 500)
        rows.append({"case": name, "status": code, "expected": expect, "ok": ok})
        if not ok:
            bad.append(f"{name}: {code}")
    time.sleep(3)
    api = H.api_ok()
    broken = {k: v for k, v in api.items() if v != 200}
    verdict = "fail" if broken or bad else "ok"
    summary = ("все ручки отвечают, мусор отклонён или безопасно принят" if verdict == "ok"
               else f"сломано: {broken or ''} {bad or ''}")
    return result("api_garbage", verdict, summary, cases=rows, api_after=api)


# ============================================================================ 3. метка времени из будущего

def s_future_timestamp() -> dict:
    H.backend_up(speed=10)
    time.sleep(15)
    victim, control = "122048", "130238"
    b = snap([victim, control])
    v = H.vehicle(victim) or {}
    bad_t = int(replay_now()) + 7200
    code = H.post("/stream/telemetry", json={
        "tr_id": victim, "timestamp": bad_t, "lat": v.get("lat", 55.7), "lon": v.get("lon", 37.6),
        "speed": 20, "heading": 90, "location_valid": True}).status_code
    time.sleep(40)  # ~7 минут воспроизведения
    a = snap([victim, control])
    now = replay_now()
    stuck = (H.epoch(a[victim]["event_time"]) or 0) >= bad_t - 1   # последняя отметка — та, из будущего
    ctrl = (H.epoch(a[control]["event_time"]) or 0) > (H.epoch(b[control]["event_time"]) or 0)
    fresh = a[victim]["as_of"] and H.epoch(a[victim]["as_of"]) <= now and H.epoch(a[victim]["as_of"]) > H.epoch(b[victim]["as_of"] or "1970-01-01T00:00:00+00:00")
    frozen = stuck and ctrl
    return result("future_timestamp", "fail" if frozen else "ok",
                  "одна отметка на 2 ч вперёд «замораживает» ТС: следующие точки отбрасываются как опоздавшие, "
                  "положение и прогноз не обновляются" if frozen else "ТС продолжает обновляться после отметки из будущего",
                  post_status=code, injected=bad_t, replay_now_after=now, before=b, after=a,
                  victim_stuck_on_future_point=stuck, control_advanced=ctrl, victim_fresh_prediction=bool(fresh))


# ============================================================================ 4. эмулятор NDTP поверх replay

def _emulator(units: list[int], seconds: float, port=18081) -> dict:
    subprocess.run(["docker", "rm", "-f", "rel-ndtp-emu"], capture_output=True)
    r = subprocess.run(["docker", "run", "-d", "--rm", "-p", f"{port}:18080", "--add-host=host.docker.internal:host-gateway",
                        "--name", "rel-ndtp-emu", "ndtp-telemetry-emulator:1.0"], capture_output=True, text=True)
    if r.returncode:
        return {"error": r.stderr.strip()[-300:]}
    api = f"http://127.0.0.1:{port}/api/config"
    for _ in range(60):
        try:
            if requests.get(api, timeout=1).ok:
                break
        except requests.RequestException:
            time.sleep(0.5)
    cfg = {"targetHost": "host.docker.internal", "targetPort": H.NDTP_ADDR[1],
           "units": [{"unitId": u, "intervalMs": 3000, "autoGenerate": True, "cells": []} for u in units]}
    since = time.time()
    requests.post(api, json=cfg, timeout=5)
    time.sleep(seconds)
    requests.post(api, json={**cfg, "units": []}, timeout=5)
    subprocess.run(["docker", "stop", "rel-ndtp-emu"], capture_output=True)
    return {"forwarded": len(H.parser_forwarded(since))}


def s_mixed_sources() -> dict:
    H.backend_up(speed=10)
    time.sleep(15)
    mapping = H.unit_mapping()
    hit = [str(mapping[u]) for u in README_UNITS]
    control = ["129964", "130238", "133957"]
    b = snap(hit + control)
    m0 = H.get("/metrics")
    emu = _emulator(README_UNITS, 20)
    time.sleep(30)
    a = snap(hit + control)
    m1 = H.get("/metrics")
    pred = advanced(b, a, "as_of")
    now = H.epoch(m1["now"])
    # ТС «застыло», если его последняя отметка не сдвинулась вслед за воспроизведением
    # или ушла в дату эмулятора (новее часов воспроизведения).
    moved = advanced(b, a, "event_time")
    stuck = [tr for tr in hit if not moved[tr] or (H.epoch(a[tr]["event_time"]) or 0) > now + 300]
    ctrl_ok = all(moved[tr] for tr in control)
    verdict = "fail" if stuck and ctrl_ok else "ok"
    rejected = m1.get("rejected_future", 0) - m0.get("rejected_future", 0)
    return result("mixed_sources", verdict,
                  f"после 20 с эмулятора по примеру из README ТС {stuck} застыли "
                  f"(отметки с текущей датой «новее» воспроизводимого дня)" if verdict == "fail"
                  else f"эмулятор не мешает воспроизведению: {rejected} его отметок с текущей датой отброшены как «из будущего»",
                  emulator=emu, before=b, after=a, positions_advanced=moved, predictions_advanced=pred, rejected_future=rejected,
                  metrics_before={k: m0[k] for k in ("now", "last_packet_at", "packets_per_min", "queue_lag_s")},
                  metrics_after={k: m1[k] for k in ("now", "last_packet_at", "packets_per_min", "queue_lag_s")})


# ============================================================================ 5. отказ ML-сервиса

def s_ml_outage() -> dict:
    H.backend_up(speed=10)
    time.sleep(15)
    ok_before = H.get("/metrics")["ml_status"]
    fails = 0
    t_stop = time.monotonic()
    H.compose("stop", "ml-service")
    t_fallback = None
    statuses = set()
    while time.monotonic() - t_stop < 40:
        try:
            ps = H.get("/predictions", timeout=3)
            statuses |= {p["status"] for p in ps}
            if t_fallback is None and any(p["status"] == "fallback" for p in ps):
                t_fallback = round(time.monotonic() - t_stop, 1)
            if not requests.get(f"{H.BACKEND}/health/live", timeout=3).ok:
                fails += 1
        except Exception:  # noqa: BLE001
            fails += 1
        time.sleep(1)
    lat_down = H.get("/metrics")["inference_latency_ms_p95"]
    t_start = time.monotonic()
    H.compose("start", "ml-service")
    t_back = None
    while time.monotonic() - t_start < 120:
        try:
            m = H.get("/metrics", timeout=3)
            ps = H.get("/predictions", timeout=3)
            if m["ml_status"] == "ok" and any(p["status"] == "model" and H.epoch(p["as_of"]) > H.epoch(m["now"]) - 60 for p in ps):
                t_back = round(time.monotonic() - t_start, 1)
                break
        except Exception:  # noqa: BLE001
            fails += 1
        time.sleep(1)
    verdict = "ok" if ok_before == "ok" and t_fallback is not None and t_back is not None and fails == 0 else "fail"
    return result("ml_outage", verdict,
                  f"без ML прогнозы идут в упрощённом режиме через {t_fallback} с, после запуска ML модель вернулась через {t_back} с; ошибок API {fails}",
                  ml_status_before=ok_before, fallback_after_s=t_fallback, recovered_after_s=t_back,
                  api_failures=fails, statuses_seen=sorted(statuses), latency_p95_ms_during=lat_down)


# ============================================================================ 6. отказ backend при потоке NDTP

class NdtpSender(threading.Thread):
    """Устройства шлют кадры раз в секунду; время — текущее, точка движется."""

    def __init__(self, units: list[int], period=1.0):
        super().__init__(daemon=True)
        self.units, self.period = units, period
        self.stop_flag = threading.Event()
        self.sent = self.errors = 0
        self.socks: dict[int, socket.socket] = {}

    def run(self):
        k = 0
        while not self.stop_flag.is_set():
            for i, u in enumerate(self.units):
                try:
                    s = self.socks.get(u)
                    if s is None:
                        s = self.socks[u] = H.ndtp_connect()
                        s.sendall(H.handshake(u))
                    s.sendall(H.ndtp_frame(u, int(time.time()), 55.70 + i * 0.01 + k * 1e-4, 37.50 + i * 0.01, 25, 0))
                    self.sent += 1
                except OSError:
                    self.errors += 1
                    self.socks.pop(u, None)
            k += 1
            time.sleep(self.period)
        for s in self.socks.values():
            s.close()


def s_backend_outage() -> dict:
    H.backend_up(autostart=False)
    # ТС без расписания: только приём и состояние, без прогнозов (дата потока ≠ дате расписания).
    units = list(H.unit_mapping())[-10:]
    sender = NdtpSender(units)
    t_begin = time.time()
    sender.start()
    time.sleep(15)
    sent_a = sender.sent
    t_down = time.time()
    H.compose("stop", "backend")
    time.sleep(20)
    sent_b = sender.sent
    t_up = time.time()
    H.compose("start", "backend")
    live_s = H.wait_live()
    time.sleep(20)
    sender.stop_flag.set()
    sender.join(5)
    lines = H.logs("ndtp-parser", t_begin)
    send_err = sum("Не удалось отправить" in ln for ln in lines)
    accepted = sum('"POST /api/v1/stream/telemetry HTTP/1.1" 200' in ln for ln in H.logs("backend", t_up))
    parser_alive = H.is_running("ndtp-parser")
    vehicles_after = sum(1 for v in H.get("/vehicles") if v["status"] == "live")
    verdict = "ok" if parser_alive and sender.errors == 0 and accepted > 0 else "fail"
    return result("backend_outage", "warn" if verdict == "ok" and send_err else verdict,
                  f"парсер пережил остановку backend, соединения устройств не рвались; за 20 с простоя потеряно "
                  f"~{send_err} отметок (буфера нет); после старта backend ({live_s:.0f} с) приём возобновился",
                  frames_sent=sender.sent, sent_before_outage=sent_a, sent_during_outage=sent_b - sent_a,
                  device_socket_errors=sender.errors, parser_send_errors=send_err, backend_accepted_after=accepted,
                  parser_alive=parser_alive, live_vehicles_after=vehicles_after, backend_live_after_start_s=round(live_s, 1))


# ============================================================================ 7. битые NDTP-кадры

def s_ndtp_garbage() -> dict:
    H.backend_up(autostart=False)
    unit = list(H.unit_mapping())[-1]
    base = int(time.time()) - 50_000
    groups: dict[int, int] = {}

    def valid(g: int, n: int) -> bytes:
        groups[g] = groups.get(g, 0) + n
        return b"".join(H.ndtp_frame(unit, base + g * 5000 + i, 55.7, 37.6) for i in range(n))

    since = time.time()
    s = H.ndtp_connect()
    s.sendall(H.handshake(unit))
    plan = [
        ("g0: 5 верных", valid(0, 5)),
        ("мусор 1 КБ", bytes(random.getrandbits(8) for _ in range(1024)).replace(b"~~", b"--")),
        ("g1: 5 верных", valid(1, 5)),
        ("кадр с неверным CRC", H.ndtp_frame(unit, base, 55.7, 37.6, bad_crc=True)),
        ("g2: 5 верных", valid(2, 5)),
        ("обрезанный кадр (20 байт)", H.ndtp_frame(unit, base, 55.7, 37.6)[:20]),
        ("g3: 5 верных", valid(3, 5)),
        ("заголовок с размером 65535", b"~~\xff\xff" + bytes(11)),
        ("g4: 200 верных", valid(4, 200)),
        ("g5: 1500 верных", valid(5, 1500)),
    ]
    for _, chunk in plan:
        s.sendall(chunk)
        time.sleep(0.2)
    s.close()
    time.sleep(25)  # одно соединение обрабатывается последовательно: дождаться, пока парсер разберёт всё
    s2 = H.ndtp_connect()
    s2.sendall(H.handshake(unit) + valid(6, 5))
    s2.close()
    # Шквал подключений: 300 сокетов по одному кадру.
    flood_ok = 0
    for i in range(300):
        try:
            c = H.ndtp_connect(timeout=3)
            c.sendall(H.ndtp_frame(unit, base + 7 * 5000 + i, 55.7, 37.6))
            c.close()
            flood_ok += 1
        except OSError:
            pass
    groups[7] = 300
    time.sleep(15)  # парсер шлёт в backend синхронно — дать догнать очередь
    got: dict[int, int] = {}
    for row in H.parser_forwarded(since):
        if int(row.get("unit_id", -1)) != unit:
            continue
        ts = int(pd.Timestamp(row["event_time"]).timestamp())
        g = (ts - base) // 5000
        got[g] = got.get(g, 0) + 1
    table = [{"group": g, "sent": n, "forwarded": got.get(g, 0)} for g, n in sorted(groups.items())]
    lost = {g: n - got.get(g, 0) for g, n in groups.items() if got.get(g, 0) < n}
    alive = H.is_running("ndtp-parser")
    verdict = "fail" if not alive else ("warn" if lost else "ok")
    return result("ndtp_garbage", verdict,
                  "парсер жив; " + ("потерь нет" if not lost else
                                    "потеряны верные кадры после битых: " + ", ".join(f"g{g}: {n}" for g, n in lost.items())),
                  groups=table, flood_connections_ok=flood_ok, parser_alive=alive)


# ============================================================================ 8. пропускная способность HTTP-приёма

async def _blast(per_vehicle: dict[str, list[dict]]) -> tuple[list[float], dict, float]:
    lat: list[float] = []
    codes: dict = {}
    conn = aiohttp.TCPConnector(limit=200)
    async with aiohttp.ClientSession(connector=conn, timeout=aiohttp.ClientTimeout(total=30)) as sess:
        async def run(rows):
            for body in rows:
                t0 = time.perf_counter()
                try:
                    async with sess.post(f"{H.BACKEND}/stream/telemetry", json=body) as r:
                        await r.read()
                        codes[r.status] = codes.get(r.status, 0) + 1
                except Exception as e:  # noqa: BLE001
                    codes[type(e).__name__] = codes.get(type(e).__name__, 0) + 1
                lat.append((time.perf_counter() - t0) * 1000)
        t0 = time.perf_counter()
        await asyncio.gather(*(run(rows) for rows in per_vehicle.values()))
        return lat, codes, time.perf_counter() - t0


def s_throughput() -> dict:
    H.backend_up(synthetic=True, autostart=False)
    df = pd.read_csv(H.SYN_DIR / "test" / "traffic.csv",
                     usecols=["tr_id", "unit_id", "event_time", "location_valid", "lat", "lon", "speed", "heading"])
    df["t"] = pd.to_datetime(df["event_time"])
    win = df[(df["t"] >= "2026-01-06 07:30") & (df["t"] < "2026-01-06 08:30")].sort_values("t")
    per: dict[str, list[dict]] = {}
    for r in win.itertuples(index=False):
        ok = str(r.location_valid).lower() == "true" and pd.notna(r.lat)
        per.setdefault(str(r.tr_id), []).append({
            "tr_id": str(r.tr_id), "timestamp": int(r.t.timestamp()) - 3 * 3600,
            "unit_id": None if pd.isna(r.unit_id) else int(r.unit_id),
            "lat": float(r.lat) if ok else None, "lon": float(r.lon) if ok else None,
            "speed": None if pd.isna(r.speed) else float(r.speed), "heading": None if pd.isna(r.heading) else float(r.heading),
            "location_valid": bool(ok), "source": "ndtp"})
    n = sum(len(v) for v in per.values())
    cpu = []
    stop = threading.Event()

    def sample():
        while not stop.is_set():
            c = H.cpu_pct("backend")
            if c is not None:
                cpu.append(c)
    th = threading.Thread(target=sample, daemon=True)
    th.start()
    lat, codes, dur = asyncio.run(_blast(per))
    stop.set()
    m = H.get("/metrics")
    rate = n / dur
    demand = n / 3600  # отметок в секунду в реальном времени (час данных)
    errors = sum(v for k, v in codes.items() if k != 200)
    verdict = "ok" if errors == 0 and (H.pct(lat, 0.95) or 1e9) < 2000 else "fail"
    return result("throughput", verdict,
                  f"{n} отметок {len(per)} ТС (час данных) приняты за {dur:.0f} с: {rate:.0f} отметок/с, "
                  f"это x{rate / demand:.0f} к реальному потоку; p95 запроса {H.pct(lat, 0.95):.0f} мс; ошибок {errors}",
                  vehicles=len(per), rows=n, seconds=round(dur, 1), rows_per_s=round(rate, 1),
                  realtime_demand_rows_per_s=round(demand, 2), headroom_x=round(rate / demand, 1),
                  request_ms={"p50": round(H.pct(lat, .5), 1), "p95": round(H.pct(lat, .95), 1), "p99": round(H.pct(lat, .99), 1),
                              "max": round(max(lat), 1)},
                  status_codes=codes, backend_cpu_pct_max=max(cpu) if cpu else None,
                  inference_ms={"p50": m["inference_latency_ms_p50"], "p95": m["inference_latency_ms_p95"]},
                  ml_status=m["ml_status"])


# ============================================================================ 9. несколько автобусов на маршруте, долгий прогон

def s_soak(minutes: float = 8, speed: float = 60) -> dict:
    clones = json.loads((H.SYN_DIR / "clones.json").read_text(encoding="utf-8"))
    clone_ids = {str(c["tr_id"]) for c in clones}
    H.backend_up(synthetic=True, speed=speed, start="06:00")
    samples = []
    t0 = time.monotonic()
    while time.monotonic() - t0 < minutes * 60:
        try:
            m = H.get("/metrics", timeout=5)
            ps = H.get("/predictions", timeout=5)
            vs = H.get("/vehicles", timeout=5)
            samples.append({
                "t_min": round((time.monotonic() - t0) / 60, 2), "replay_now": m["now"], "mem_mb": H.mem_mb("backend"),
                "cpu_pct": H.cpu_pct("backend"), "queue_lag_s": m["queue_lag_s"], "latency_p95_ms": m["inference_latency_ms_p95"],
                "packets_per_min": m["packets_per_min"], "vehicles_live": m["vehicles_live"], "predictions": len(ps),
                "clone_predictions": sum(str(p["tr_id"]) in clone_ids for p in ps),
                "clones_matched": sum(str(v["tr_id"]) in clone_ids and bool(v.get("route_pattern_id")) for v in vs),
                "clones_live": sum(str(v["tr_id"]) in clone_ids and v["status"] == "live" for v in vs),
                "n_verified": m["n_verified"], "ml_status": m["ml_status"]})
        except Exception as e:  # noqa: BLE001
            samples.append({"t_min": round((time.monotonic() - t0) / 60, 2), "error": f"{type(e).__name__}: {e}"})
        time.sleep(20)
    ver = H.get("/predictions/verified?all=true&limit=20000", timeout=30)
    def mae(rows):
        return round(sum(abs(r["prediction_delay_s"] - r["outcome_delay_s"]) for r in rows) / len(rows), 1) if rows else None
    orig = [r for r in ver if str(r["tr_id"]) not in clone_ids]
    cl = [r for r in ver if str(r["tr_id"]) in clone_ids]
    inc = H.get("/incidents")
    ok = [s for s in samples if "error" not in s]
    lag = [s["queue_lag_s"] for s in ok]
    mem = [s["mem_mb"] for s in ok if s["mem_mb"]]
    span = (ok[-1]["t_min"] - ok[0]["t_min"]) if len(ok) > 1 else 1
    growth = round((mem[-1] - mem[0]) / span, 1) if len(mem) > 1 else None
    errors = len(samples) - len(ok)
    verdict = "ok" if errors == 0 and cl and max(lag) < 120 else ("warn" if errors == 0 and cl else "fail")
    return result("soak", verdict,
                  f"{len(clones) + 13} ТС (по 4 на маршруте), x{speed:g}, {minutes:g} мин: отставание обработки до {max(lag)} с, "
                  f"память {mem[0]:.0f}→{mem[-1]:.0f} МБ ({growth:+} МБ/мин); прогнозы по копиям: {len(cl)} проверено, "
                  f"MAE {mae(cl)} с против {mae(orig)} с у исходных ТС",
                  samples=samples, verified_clones=len(cl), verified_orig=len(orig), mae_clones=mae(cl), mae_orig=mae(orig),
                  incidents_on_clones=sum(str(i["tr_id"]) in clone_ids for i in inc), incidents_total=len(inc),
                  mem_growth_mb_per_min=growth, api_errors=errors)


SCENARIOS = {
    "cold_start": s_cold_start, "api_garbage": s_api_garbage, "future_timestamp": s_future_timestamp,
    "mixed_sources": s_mixed_sources, "ml_outage": s_ml_outage, "backend_outage": s_backend_outage,
    "ndtp_garbage": s_ndtp_garbage, "throughput": s_throughput, "soak": s_soak,
}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("scenarios", nargs="*", help=f"по умолчанию — все: {', '.join(SCENARIOS)}")
    ap.add_argument("--keep", action="store_true", help="не возвращать backend в режим по умолчанию")
    args = ap.parse_args()
    RESULTS.mkdir(exist_ok=True)
    unknown = [n for n in args.scenarios if n not in SCENARIOS]
    if unknown:
        ap.error(f"нет сценариев: {unknown}")
    out = []
    for name in args.scenarios or list(SCENARIOS):
        print(f"\n=== {name}", flush=True)
        t0 = time.monotonic()
        try:
            r = SCENARIOS[name]()
        except Exception as e:  # noqa: BLE001
            r = result(name, "error", f"{type(e).__name__}: {e}")
        r["duration_s"] = round(time.monotonic() - t0, 1)
        (RESULTS / f"{name}.json").write_text(json.dumps(r, ensure_ascii=False, indent=1, default=str), encoding="utf-8")
        print(f"[{r['verdict'].upper()}] {r['summary']}", flush=True)
        out.append(r)
    if not args.keep:
        H.compose("up", "-d")
        H.backend_up()
    print("\n=== сводка")
    for r in out:
        print(f"{r['verdict'].upper():5} {r['scenario']:18} {r['summary']}")


if __name__ == "__main__":
    main()
