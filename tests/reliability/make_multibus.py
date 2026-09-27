"""Синтетический датасет «несколько автобусов на маршруте».

В датасете хакатона у каждого ТС свой маршрут. В реальности по маршруту идут несколько
машин с интервалом. Скрипт добавляет к каждому ТС с расписанием ``--clones`` копий:

* своё ``tr_id`` / ``unit_id`` / ``tt_action_item_id`` / ``packet_id``;
* расписание и факты сдвинуты на интервал ``k * headway`` (+ случайная добавка до ±60 с);
* GPS-трек сдвинут на тот же интервал, координаты с шумом ~5 м;
* ``network.json`` дополняется геометрией исходного маршрута, чтобы копии рисовались на карте.

Пример:
    python tests/reliability/make_multibus.py --data-dir ../dataset --out ../synthetic/multibus --clones 3
"""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]


def clone_id(base: int, k: int) -> int:
    """91122048: «9», номер копии, исходный tr_id — видно, от какого ТС копия."""
    return int(f"9{k}{base}")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--data-dir", type=Path, required=True, help="исходный датасет (папка с test/)")
    ap.add_argument("--out", type=Path, required=True, help="куда писать синтетический датасет")
    ap.add_argument("--split", default="test")
    ap.add_argument("--clones", type=int, default=3, help="копий на каждое ТС с расписанием")
    ap.add_argument("--headway-min", type=float, default=7.0, help="интервал между машинами, мин")
    ap.add_argument("--network", type=Path, default=ROOT / "dashboard" / "data" / "network.json")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()
    rng = np.random.default_rng(args.seed)

    src = args.data_dir / args.split
    dst = args.out / args.split
    dst.mkdir(parents=True, exist_ok=True)
    sched = pd.read_csv(src / "schedule.csv")
    traffic = pd.read_csv(src / "traffic.csv")
    base_trs = sorted(sched["tr_id"].unique())
    units = traffic.dropna(subset=["unit_id"]).groupby("tr_id")["unit_id"].first()

    s_parts, t_parts, clones = [sched], [traffic], []
    for tr in base_trs:
        s0 = sched[sched["tr_id"] == tr]
        t0 = traffic[traffic["tr_id"] == tr]
        for k in range(1, args.clones + 1):
            shift = pd.to_timedelta(k * args.headway_min * 60 + rng.uniform(-60, 60), unit="s")
            new_tr = clone_id(int(tr), k)
            s = s0.copy()
            s["tr_id"] = new_tr
            s["tt_action_item_id"] = s["tt_action_item_id"] + k * 10**11
            for col in ("time_begin", "time_fact_begin"):
                s[col] = (pd.to_datetime(s[col]) + shift).dt.strftime("%Y-%m-%d %H:%M:%S")
            s_parts.append(s)

            t = t0.copy()
            t["tr_id"] = new_tr
            t["unit_id"] = clone_id(int(units.get(tr, 0)), k)
            t["packet_id"] = t["packet_id"] + k * 10**15
            for col in ("event_time", "gps_time", "receive_time"):
                t[col] = (pd.to_datetime(t[col]) + shift).dt.strftime("%Y-%m-%d %H:%M:%S.%f")
            ok = t["lat"].notna()
            t.loc[ok, "lat"] += rng.normal(0, 5 / 111_320, ok.sum())
            t.loc[ok, "lon"] += rng.normal(0, 5 / 62_500, ok.sum())
            t_parts.append(t)
            clones.append({"tr_id": new_tr, "base_tr_id": int(tr), "shift_s": round(shift.total_seconds())})

    out_s = pd.concat(s_parts, ignore_index=True)
    out_t = pd.concat(t_parts, ignore_index=True)
    out_s.to_csv(dst / "schedule.csv", index=False)
    out_t.to_csv(dst / "traffic.csv", index=False)

    # Сеть: копия маршрута исходного ТС под новым tr_id.
    net = json.loads(args.network.read_text(encoding="utf-8"))
    by_tr = {int(r["tr_id"]): r for r in net["routes"]}
    for c in clones:
        r = by_tr.get(c["base_tr_id"])
        if r:
            net["routes"].append({**r, "tr_id": c["tr_id"], "route_id": f"R{c['tr_id']}",
                                  "name": f"{r.get('name', '')} (копия {str(c['tr_id'])[1]})"})
    ref = args.out / "refdata"
    ref.mkdir(parents=True, exist_ok=True)
    (ref / "network.json").write_text(json.dumps(net, ensure_ascii=False), encoding="utf-8")
    (args.out / "clones.json").write_text(json.dumps(clones, ensure_ascii=False, indent=1), encoding="utf-8")
    for f in ("labels",):
        if (args.data_dir / f).exists() and not (args.out / f).exists():
            shutil.copytree(args.data_dir / f, args.out / f)
    print(f"ТС с расписанием: {len(base_trs)} + копий {len(clones)} = {out_s['tr_id'].nunique()}; "
          f"посещений {len(out_s)}; записей телеметрии {len(out_t)} (было {len(traffic)})")


if __name__ == "__main__":
    main()
