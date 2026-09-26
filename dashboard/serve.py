"""Запуск дашборда одной командой, без Docker и без внешних зависимостей.

    python dashboard/serve.py                 # http://127.0.0.1:8080, режим REPLAY
    python dashboard/serve.py --open          # и открыть в браузере
    python dashboard/serve.py --api http://127.0.0.1:8000/api/v1   # сразу подключиться к backend

Отличие от ``python -m http.server``: поддержаны HTTP Range-запросы, без которых браузер
не может читать подложку карты из ``data/basemap/moscow.pmtiles``.
"""

from __future__ import annotations

import argparse
import os
import re
import webbrowser
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent
RANGE_RE = re.compile(r"bytes=(\d*)-(\d*)$")


class RangeHandler(SimpleHTTPRequestHandler):
    """Статика с поддержкой ``Range: bytes=a-b`` (ответ 206 Partial Content)."""

    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".js": "text/javascript",
        ".json": "application/json",
        ".pbf": "application/x-protobuf",
        ".pmtiles": "application/octet-stream",
    }

    def log_message(self, fmt, *args):  # тихий режим: сотни Range-запросов засоряют консоль
        pass

    def end_headers(self):
        self.send_header("Accept-Ranges", "bytes")
        if self.path.split("?")[0].endswith("config.js"):
            self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def send_head(self):
        self._remaining = None
        rng = self.headers.get("Range")
        path = self.translate_path(self.path)
        m = RANGE_RE.match(rng.strip()) if rng else None
        if not m or os.path.isdir(path):
            return super().send_head()
        try:
            f = open(path, "rb")
        except OSError:
            self.send_error(404, "File not found")
            return None
        size = os.fstat(f.fileno()).st_size
        first, last = m.groups()
        if first == "":  # суффикс: последние N байт
            start, end = max(0, size - int(last or 0)), size - 1
        else:
            start, end = int(first), min(int(last) if last else size - 1, size - 1)
        if start > end or start >= size:
            f.close()
            self.send_response(416)
            self.send_header("Content-Range", f"bytes */{size}")
            self.end_headers()
            return None
        self.send_response(206)
        self.send_header("Content-Type", self.guess_type(path))
        self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.send_header("Content-Length", str(end - start + 1))
        self.end_headers()
        f.seek(start)
        self._remaining = end - start + 1
        return f

    def copyfile(self, source, outputfile):
        if self._remaining is None:
            return super().copyfile(source, outputfile)
        left = self._remaining
        while left > 0:
            chunk = source.read(min(1 << 16, left))
            if not chunk:
                break
            outputfile.write(chunk)
            left -= len(chunk)


class Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Браузер обрывает ненужные Range-запросы при перемещении карты — это не ошибка.
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--api", help="адрес backend для режима LIVE (добавляется в ссылку как ?api=...)")
    ap.add_argument("--open", action="store_true", help="открыть дашборд в браузере")
    args = ap.parse_args()

    if not (ROOT / "data" / "replay.js").exists():
        print("! Нет data/replay.js — соберите: python dashboard/tools/build_fixtures.py --data-dir <dataset>")
    if not (ROOT / "data" / "basemap" / "moscow.pmtiles").exists():
        print("! Нет подложки карты — скачайте: python dashboard/tools/fetch_basemap.py")

    handler = partial(RangeHandler, directory=str(ROOT))
    srv = None
    for port in range(args.port, args.port + 20):
        try:
            srv = Server((args.host, port), handler)
            break
        except OSError as e:
            # Порт занят другим процессом (на Windows Docker даёт WinError 10013, а не 10048).
            print(f"Порт {port} занят ({e.strerror or e}), пробую {port + 1}", flush=True)
    if srv is None:
        raise SystemExit(f"Нет свободного порта в диапазоне {args.port}–{args.port + 19}: укажите --port")
    url = f"http://{args.host}:{srv.server_address[1]}/" + (f"?api={args.api}" if args.api else "")
    print(f"Дашборд: {url}   (Ctrl+C — остановить)", flush=True)
    if args.open:
        webbrowser.open(url)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
