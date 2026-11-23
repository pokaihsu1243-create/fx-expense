# -*- coding: utf-8 -*-
"""每天由 GitHub Actions 執行：向兆豐官網抓各幣別即期買入匯率，累積存到 data/。

data/meta.json          更新時間、幣別清單、各幣別資料定案到哪一天（coveredThrough）
data/rates/<幣別>.json   {類別: {"YYYY-MM-DD": 買入匯率}}，舊資料保留（兆豐官網只留一年，這裡會一直累積）

第一次抓近一年，之後每次只抓最近 45 天合併進去。
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

import server

DATA = Path(__file__).resolve().parent / "data"
RECENT_DAYS = 45


def read_json(p, default):
    try:
        return json.loads(p.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def write_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, ensure_ascii=False, indent=1) + "\n", encoding="utf-8", newline="\n")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    now = server.tw_now()
    today, cov = now.date(), server.covered_through(now).isoformat()
    old_meta = read_json(DATA / "meta.json", {})
    old_cov = {c["code"]: c.get("coveredThrough") for c in old_meta.get("currencies", [])}

    currencies, failed = [], []
    for c in server.MEGA.get_currencies():
        code = c["code"]
        f = DATA / "rates" / f"{code}.json"
        old = read_json(f, {})
        days = RECENT_DAYS if any(old.values()) else 364
        try:
            new = server.fetch_currency(code, today - timedelta(days=days), today, pause=0.3)
        except Exception as e:  # 單一幣別失敗不影響其他幣別，定案日期維持舊的
            failed.append(f"{code}: {e}")
            new = None
        if new is not None and any(new.values()):
            merged = {}
            for cat in server.CATEGORIES:
                m = dict(old.get(cat, {}))
                m.update(new.get(cat, {}))
                merged[cat] = dict(sorted(m.items()))
            write_json(f, merged)
            c_cov = cov
            print(f"{code}: 最新 {max(merged['LastBusinessDayRate'] or {'-': 0})}，共 {len(merged['LastBusinessDayRate'])} 天")
        else:
            if new is not None:
                failed.append(f"{code}: 查無資料")
            c_cov = old_cov.get(code)
            if not c_cov:
                continue
        currencies.append({"code": code, "label": c["label"], "coveredThrough": c_cov})

    if not currencies:
        print("全部幣別都失敗：", *failed, sep="\n")
        return 1
    write_json(DATA / "meta.json", {
        "updatedAt": now.strftime("%Y-%m-%d %H:%M"),
        "mode": "static",
        "categories": server.CATEGORIES,
        "currencies": currencies,
    })
    if failed:
        print("部分幣別失敗：", *failed, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
