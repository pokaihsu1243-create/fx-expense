# -*- coding: utf-8 -*-
"""每天由 GitHub Actions 執行：向兆豐官網抓各幣別即期買入匯率，累積存到 data/。

data/meta.json          更新時間、幣別清單、各幣別資料定案到哪一天（coveredThrough）
data/rates/<幣別>.json   {類別: {"YYYY-MM-DD": 買入匯率}}，只保留最近 3 個月（使用者 2026-11-23 決定），更早的自動刪除

每次抓最近 45 天合併進去；缺資料時抓滿保留期間。
"""
import json
import sys
from datetime import timedelta
from pathlib import Path

import server

DATA = Path(__file__).resolve().parent / "data"
RECENT_DAYS = 45
KEEP_MONTHS = 3      # 保留最近幾個月
BUFFER_DAYS = 15     # 多留幾天，讓保留期間第一天遇到假日時，仍找得到前一個營業日


def keep_from(today):
    """保留起日＝今天往前 3 個月（同一天，月底自動調整）再往前 15 天。"""
    y, m = today.year, today.month - KEEP_MONTHS
    while m < 1:
        y, m = y - 1, m + 12
    last = [31, 29 if y % 4 == 0 and (y % 100 or y % 400 == 0) else 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31][m - 1]
    return today.replace(year=y, month=m, day=min(today.day, last)) - timedelta(days=BUFFER_DAYS)


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
    cutoff = keep_from(today)
    cutoff_s = cutoff.isoformat()
    old_meta = read_json(DATA / "meta.json", {})
    old_cov = {c["code"]: c.get("coveredThrough") for c in old_meta.get("currencies", [])}

    currencies, failed = [], []
    for c in server.MEGA.get_currencies():
        code = c["code"]
        f = DATA / "rates" / f"{code}.json"
        old = read_json(f, {})
        have = sorted(d for cat in old.values() for d in cat)
        start = today - timedelta(days=RECENT_DAYS)
        if not have or have[0] > (cutoff + timedelta(days=7)).isoformat():  # 起日可能剛好遇到連假，留 7 天彈性
            start = cutoff  # 沒資料或前段不完整：抓滿保留期間
        try:
            new = server.fetch_currency(code, start, today, pause=0.3)
        except Exception as e:  # 單一幣別失敗不影響其他幣別，定案日期維持舊的
            failed.append(f"{code}: {e}")
            new = None
        if new is not None and any(new.values()):
            merged = {}
            for cat in server.CATEGORIES:
                m = dict(old.get(cat, {}))
                m.update(new.get(cat, {}))
                merged[cat] = {d: v for d, v in sorted(m.items()) if d >= cutoff_s}  # 超過 3 個月的刪掉
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
        "keepMonths": KEEP_MONTHS,
        "keepFrom": cutoff_s,
        "categories": server.CATEGORIES,
        "currencies": currencies,
    })
    if failed:
        print("部分幣別失敗：", *failed, sep="\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
