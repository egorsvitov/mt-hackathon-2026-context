"""Общие инструменты тестов надёжности: docker compose, API backend, NDTP-кадры, замеры."""

from __future__ import annotations

import json
import os
import re
import socket
import struct
import subprocess
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[2]
BACKEND = os.getenv("REL_BACKEND", "http://127.0.0.1:18000/api/v1")
DASHBOARD = os.getenv("REL_DASHBOARD", "http://127.0.0.1:18090")
NDTP_ADDR = ("127.0.0.1", int(os.getenv("REL_NDTP_PORT", "19201")))
PROJECT = "mt-hackathon-2026-context"
SYN_DIR = Path(os.getenv("REL_SYN_DIR", ROOT.parent / "synthetic" / "multibus")).resolve()
OVERRIDE = Path(__file__).with_name("compose.synthetic.yml")


# ---------------------------------------------------------------------------- docker compose

def compose(*args: str, env: dict | None = None, files: tuple[Path, ...] = (), check=True, timeout=900) -> str:
    cmd = ["docker", "compose", "-f", str(ROOT / "docker-compose.yml")]
    for f in files:
        cmd += ["-f", str(f)]
    r = subprocess.run(cmd + list(args), cwd=ROOT, env={**os.environ, **(env or {})},
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout)
    if check and r.returncode:
        raise RuntimeError(f"docker compose {' '.join(args)}: {r.stderr[-800:]}")
    return r.stdout + r.stderr


def container(service: str) -> str:
    return f"{PROJECT}-{service}-1"


def backend_up(*, synthetic=False, autostart=True, speed=1.0, start="08:30", timeout=180) -> float:
    """Пересоздать backend с нужным окружением; вернуть время до /health/live, с."""
    env = {"REPLAY_AUTOSTART": str(autostart).lower(), "REPLAY_SPEED": str(speed), "REPLAY_START": start}
    files = ()
    if synthetic:
        env["SYN_DIR"] = str(SYN_DIR)
        files = (OVERRIDE,)
    t0 = time.monotonic()
    compose("up", "-d", "--no-deps", "--force-recreate", "backend", env=env, files=files)
    wait_live(timeout)
    return time.monotonic() - t0


def wait_live(timeout=120) -> float:
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        try:
            if requests.get(f"{BACKEND}/health/live", timeout=2).ok:
                return time.monotonic() - t0
        except requests.RequestException:
            pass
        time.sleep(0.5)
    raise TimeoutError("backend не поднялся")


def mem_mb(service: str) -> float | None:
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.MemUsage}}", container(service)],
                       capture_output=True, text=True)
    m = re.match(r"([\d.]+)\s*([KMG]i?B)", r.stdout.strip())
    if not m:
        return None
    k = {"KiB": 1 / 1024, "KB": 1 / 1000, "MiB": 1, "MB": 1, "GiB": 1024, "GB": 1000}[m.group(2)]
    return float(m.group(1)) * k


def cpu_pct(service: str) -> float | None:
    r = subprocess.run(["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container(service)],
                       capture_output=True, text=True)
    try:
        return float(r.stdout.strip().rstrip("%"))
    except ValueError:
        return None


def logs(service: str, since: float) -> list[str]:
    """Строки лога сервиса с момента since (epoch)."""
    r = subprocess.run(["docker", "logs", "--since", str(int(since)), container(service)],
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    return (r.stdout + r.stderr).splitlines()


def is_running(service: str) -> bool:
    r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", container(service)], capture_output=True, text=True)
    return r.stdout.strip() == "true"


# ---------------------------------------------------------------------------- API

def get(path: str, timeout=10):
    r = requests.get(f"{BACKEND}{path}", timeout=timeout)
    r.raise_for_status()
    return r.json()


def post(path: str, timeout=10, **kw):
    return requests.post(f"{BACKEND}{path}", timeout=timeout, **kw)


def vehicle(tr) -> dict | None:
    return next((v for v in get("/vehicles") if str(v["tr_id"]) == str(tr)), None)


def prediction(tr) -> dict | None:
    return next((p for p in get("/predictions") if str(p["tr_id"]) == str(tr)), None)


def api_ok() -> dict:
    """Все ручки дашборда отвечают 200 и валидным JSON."""
    out = {}
    for p in ("/health", "/health/ready", "/network", "/vehicles", "/predictions", "/incidents", "/metrics", "/config"):
        try:
            r = requests.get(f"{BACKEND}{p}", timeout=10)
            r.json()
            out[p] = r.status_code
        except Exception as e:  # noqa: BLE001
            out[p] = f"{type(e).__name__}"
    return out


def epoch(iso: str | None) -> float | None:
    if not iso:
        return None
    from datetime import datetime
    return datetime.fromisoformat(iso).timestamp()


# ---------------------------------------------------------------------------- NDTP

def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def ndtp_frame(unit_id: int, ts: int, lat: float, lon: float, speed=20, course=90, valid=True,
               service=1, mtype=101, request_id=0, bad_crc=False) -> bytes:
    """Кадр NPL + NPH + ячейка G6CellNav00 — формат ndtp-parser/protocol.py."""
    extra = (0x80 if valid else 0) | (0x40 if lon >= 0 else 0) | (0x20 if lat >= 0 else 0)
    nav = struct.pack("<IIIBBHHHHHBB", ts, int(abs(lon) * 1e7), int(abs(lat) * 1e7), extra, 0,
                      int(speed), int(speed), int(course) % 360, 0, 150, 9, 10)
    body = struct.pack("<HHHI", service, mtype, 0, request_id) + bytes([0, 0]) + nav
    crc = crc16_modbus(body)
    crc = ((crc & 0xFF) << 8) | (crc >> 8)
    if bad_crc:
        crc ^= 0x1234
    npl = b"\x7e\x7e" + struct.pack("<HHH", len(body), 0, crc) + bytes([0x02]) + struct.pack("<IH", unit_id, request_id & 0xFFFF)
    return npl + body


def handshake(unit_id: int) -> bytes:
    body = struct.pack("<HHHI", 0, 100, 0, 0)
    crc = crc16_modbus(body)
    crc = ((crc & 0xFF) << 8) | (crc >> 8)
    return b"\x7e\x7e" + struct.pack("<HHH", len(body), 0, crc) + bytes([0x02]) + struct.pack("<IH", unit_id, 0) + body


def ndtp_connect(timeout=5) -> socket.socket:
    s = socket.create_connection(NDTP_ADDR, timeout=timeout)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    return s


def parser_forwarded(since: float) -> list[dict]:
    """Строки телеметрии, которые парсер разобрал и отправил в backend (он печатает их JSON).

    Потоки парсера пишут в stdout одновременно, строки склеиваются — поэтому JSON-объекты
    ищутся по всему тексту лога, а не построчно.
    """
    text = "\n".join(logs("ndtp-parser", since))
    out = []
    for m in re.finditer(r'\{"[^{}]*"event_time"[^{}]*\}', text):
        try:
            out.append(json.loads(m.group(0)))
        except json.JSONDecodeError:
            pass
    return out


def unit_mapping() -> dict[int, int]:
    m = json.loads((ROOT / "ndtp-parser" / "unit_mapping.json").read_text(encoding="utf-8"))
    return {int(k): int(v) for k, v in m.items()}


def pct(values, q):
    if not values:
        return None
    v = sorted(values)
    return v[min(len(v) - 1, int(len(v) * q))]
