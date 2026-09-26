"""Скачивает подложку карты: вырезку OpenStreetMap по району маршрутов в ``data/basemap/moscow.pmtiles``.

Источник — открытая ежедневная сборка Protomaps (данные OSM, лицензия ODbL). Скачиваются
только нужные тайлы (HTTP Range), это ~60 МБ и меньше минуты. Нужен один раз: дальше карта
работает без интернета.

    python dashboard/tools/fetch_basemap.py                       # свежая сборка, район Москвы, зум до 14
    python dashboard/tools/fetch_basemap.py --source moscow.pmtiles   # взять готовый файл/зеркало команды

Для вырезки используется утилита ``pmtiles`` (go-pmtiles): берётся из PATH или скачивается
с GitHub в ``tools/.cache/``.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE.parent / "data" / "basemap" / "moscow.pmtiles"
CACHE = HERE / ".cache"
BBOX = "36.95,55.42,38.05,56.08"  # все маршруты датасета с запасом (Зеленоград — Видное, Внуково — Балашиха)
PMTILES_VERSION = "1.31.2"
BUILDS = "https://build-metadata.protomaps.dev/builds.json"


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "mt-dashboard/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def pmtiles_cli() -> str:
    """Путь к утилите pmtiles: из PATH или скачанная в tools/.cache."""
    found = shutil.which("pmtiles")
    if found:
        return found
    system = {"Windows": "Windows", "Linux": "Linux", "Darwin": "Darwin"}[platform.system()]
    arch = "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"
    exe = CACHE / ("pmtiles.exe" if system == "Windows" else "pmtiles")
    if exe.exists():
        return str(exe)
    ext = "zip" if system in ("Windows", "Darwin") else "tar.gz"
    url = (f"https://github.com/protomaps/go-pmtiles/releases/download/v{PMTILES_VERSION}/"
           f"go-pmtiles_{PMTILES_VERSION}_{system}_{arch}.{ext}")
    print(f"Скачиваю pmtiles: {url}")
    data = _get(url)
    CACHE.mkdir(parents=True, exist_ok=True)
    if ext == "zip":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            name = next(n for n in z.namelist() if n.endswith(exe.name))
            exe.write_bytes(z.read(name))
    else:
        with tarfile.open(fileobj=io.BytesIO(data)) as t:
            member = next(m for m in t.getmembers() if m.name.endswith("pmtiles"))
            exe.write_bytes(t.extractfile(member).read())
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    return str(exe)


def latest_build() -> str:
    builds = json.loads(_get(BUILDS))
    key = max(b["key"] for b in builds if b["key"].endswith(".pmtiles"))
    return f"https://build.protomaps.com/{key}"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--source", help="URL или файл .pmtiles; по умолчанию — последняя сборка Protomaps")
    ap.add_argument("--bbox", default=BBOX, help="минДолгота,минШирота,максДолгота,максШирота")
    ap.add_argument("--maxzoom", type=int, default=14, help="14 достаточно: MapLibre растягивает векторные тайлы")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    source = args.source or latest_build()
    cli = pmtiles_cli()
    print(f"Вырезаю {args.bbox}, зум ≤ {args.maxzoom} из {source}")
    env = {**os.environ, "GODEBUG": "http2client=0"}  # у части сетей HTTP/2 к сборке обрывается
    tmp = out.with_suffix(".part")
    subprocess.run([cli, "extract", source, str(tmp), f"--bbox={args.bbox}", f"--maxzoom={args.maxzoom}"],
                   check=True, env=env)
    tmp.replace(out)
    print(f"Готово: {out} ({out.stat().st_size / 1e6:.0f} МБ)")


if __name__ == "__main__":
    sys.exit(main())
