# -*- coding: utf-8 -*-
"""出差報支匯率查詢：向兆豐銀行官網「歷史匯率」取消費當日的即期買入匯率，換算台幣。
只用標準函式庫。啟動：python server.py（或打包後的 exe）→ http://localhost:8728
"""
import json
import re
import sys
import threading
import time
import urllib.request
import uuid
import http.cookiejar
import webbrowser
from datetime import date, datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8728
HERE = Path(getattr(sys, "_MEIPASS", "") or Path(__file__).resolve().parent)  # 打包成 exe 時 index.html 在暫存解壓目錄
BASE = "https://www.megabank.com.tw"
PAGE = BASE + "/personal/savings/foreign-service/forex"
QS = "?sc_lang=zh-TW&sc_site=bank-zh-tw&dic_lang=zh-TW"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")

CATEGORIES = {
    "LastBusinessDayRate": "營業日最後一次匯率(約16:00)",
    "FinalBusinessHourRate": "營業時間最後一次匯率(約15:30)",
    "FirstBusinessDayRate": "營業時間第一次匯率",
}
RATE_TYPES = {"Cash": "現金", "Spot": "即期"}
LOOKBACK_DAYS = 15  # 遇假日往前找營業日，最多回看幾天
TWD = "TWD"  # 新台幣：不查匯率、不換算（2026-11-17 新增，給國內／機場台幣消費用）


class Mega:
    """兆豐官網連線（token 與 cookie 快取 10 分鐘）。"""

    def __init__(self):
        self.lock = threading.Lock()
        self.opener = None
        self.token = None
        self.item_id = None
        self.currencies = None
        self.loaded_at = 0.0

    def _load(self):
        cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
        self.opener.addheaders = [("User-Agent", UA)]
        html = self.opener.open(PAGE, timeout=40).read().decode("utf-8", "replace")
        m1 = re.search(r'name="__RequestVerificationToken"[^>]*value="([^"]+)"', html)
        m2 = re.search(r'history-rate-panel-01-([0-9a-f]{32})', html)
        if not m1 or not m2:
            raise RuntimeError("兆豐官網頁面格式變了，找不到歷史匯率查詢區塊")
        self.token, self.item_id = m1.group(1), m2.group(1)
        self.loaded_at = time.time()
        cur = self._post("GetCurrencyOptions", {"itemId": self.item_id})
        self.currencies = [
            {"code": c["value"], "label": c["label"],
             "id": c["customData"]["id"], "itemId": c["customData"]["itemId"]}
            for c in cur["list"]
        ]

    def _post(self, name, obj):
        req = urllib.request.Request(
            f"{BASE}/api/client/HistoricalExchangeRateList/{name}{QS}",
            data=json.dumps(obj).encode("utf-8"),
            headers={"Content-Type": "application/json; charset=utf-8",
                     "X-Requested-With": "XMLHttpRequest", "Referer": PAGE})
        return json.loads(self.opener.open(req, timeout=40).read().decode("utf-8", "replace"))

    def ensure(self):
        if not self.opener or time.time() - self.loaded_at > 600:
            self._load()

    def get_currencies(self):
        with self.lock:
            self.ensure()
            return self.currencies

    def query(self, code, rate_type, category, start, end):
        """回傳 {date: (買入, 賣出)}，只含有報價的日子。"""
        with self.lock:
            self.ensure()
            cur = next((c for c in self.currencies if c["code"] == code), None)
            if not cur:
                raise ValueError(f"兆豐沒有 {code} 這個幣別的歷史匯率")
            params = {
                "__RequestVerificationToken": self.token,
                "currencyId": cur["id"], "currencyItemId": cur["itemId"],
                "periodType": "customRange",
                "startDateStr": start.isoformat(), "endDateStr": end.isoformat(),
                "singleDateStr": None, "categoryCode": category,
                "rangeType": "range", "rateType": rate_type,
                "specificTodayQuery": False,
                }
            res = None
            for attempt in range(3):  # 偶爾回空的，換新的 requestId 再問一次
                params["requestId"] = str(uuid.uuid4())
                res = self._post("Query", params)
                if ((res or {}).get("data") or {}).get("rates"):
                    break
                time.sleep(0.8)
        out = {}
        for r in ((res or {}).get("data") or {}).get("rates") or []:
            d = date.fromisoformat(r["date"][:10])
            out[d] = (r.get("bidRateText"), r.get("askRateText"))
        return out


MEGA = Mega()


def lookup(items, category):
    """會計規則（依 2024/11 會計報支表）：兆豐、即期、銀行買入、消費當天；
    假日往前取最近營業日；各類別小計＝每筆先四捨五入到元後加總（2026-11-17 起；原為未進位加總後再四捨五入）。"""
    rate_type = "Spot"
    if category not in CATEGORIES:
        raise ValueError("查詢類別不正確")
    today = date.today()
    parsed = []
    for i, it in enumerate(items, 1):
        try:
            d = date.fromisoformat(str(it.get("date", "")).strip())
        except ValueError:
            raise ValueError(f"第 {i} 列日期格式不對")
        code = str(it.get("currency", "")).strip().upper() or "USD"
        if d > today:
            raise ValueError(f"第 {i} 列日期 {d} 還沒到，查不到匯率")
        if code != TWD and d < today - timedelta(days=365):
            raise ValueError(f"第 {i} 列日期 {d} 超過一年，兆豐官網只保留最近一年的歷史匯率，查不到")
        amt_raw = str(it.get("amount", "")).replace(",", "").strip()
        try:
            amt = Decimal(amt_raw) if amt_raw else None
        except Exception:
            raise ValueError(f"第 {i} 列金額不是數字")
        parsed.append({"date": d, "currency": code, "amount": amt,
                       "group": str(it.get("group", "")).strip() or "其他",
                       "note": str(it.get("note", "")).strip()})

    tables = {}
    for code in sorted({p["currency"] for p in parsed} - {TWD}):  # 新台幣不用查匯率
        ds = [p["date"] for p in parsed if p["currency"] == code]
        start, end = min(ds) - timedelta(days=LOOKBACK_DAYS), max(ds)
        if (end - start).days > 360:
            raise ValueError(f"{code} 的日期跨度超過一年，請分次查")
        tables[code] = MEGA.query(code, rate_type, category, start, end)

    one = Decimal(1)
    groups, order, rate_tbl, missing = {}, [], {}, 0
    for p in parsed:
        tbl = tables.get(p["currency"], {})
        used = p["date"]
        while used not in tbl and (p["date"] - used).days < LOOKBACK_DAYS:
            used -= timedelta(days=1)
        row = {"date": p["date"].isoformat(), "currency": p["currency"], "note": p["note"],
               "amount": str(p["amount"]) if p["amount"] is not None else "",
               "usedDate": None, "rate": None, "twd": None, "remark": ""}
        exact = None
        if p["currency"] == TWD:  # 新台幣消費：不換算，直接四捨五入到元
            row.update(usedDate=p["date"].isoformat(), rate="1")
            if p["amount"] is not None:
                exact = p["amount"]
                row["twd"] = int(exact.quantize(one, rounding=ROUND_HALF_UP))
        elif used in tbl and tbl[used][0]:
            buy = tbl[used][0]
            row.update(usedDate=used.isoformat(), rate=buy)
            key = (p["currency"], used)
            rate_tbl.setdefault(key, {"currency": p["currency"], "rateDate": used.isoformat(),
                                      "rate": buy, "forDates": []})
            if row["date"] not in rate_tbl[key]["forDates"]:
                rate_tbl[key]["forDates"].append(row["date"])
            if used != p["date"]:
                row["remark"] = f"{p['date'].strftime('%m/%d')} 無牌告，採前一營業日 {used.strftime('%m/%d')}"
            if p["amount"] is not None:
                exact = p["amount"] * Decimal(buy)
                row["twd"] = int(exact.quantize(one, rounding=ROUND_HALF_UP))
        else:
            row["remark"] = "查無匯率"
            missing += 1
        if p["group"] not in groups:
            groups[p["group"]] = {"name": p["group"], "rows": [], "_sum": Decimal(0)}
            order.append(p["group"])
        g = groups[p["group"]]
        g["rows"].append(row)
        if exact is not None:
            # 2026-11-17 使用者決定：每筆先四捨五入到元，小計＝各筆整數加總（每列與小計對得起來）
            g["_sum"] += Decimal(row["twd"])

    out_groups, total = [], 0
    for name in order:
        g = groups[name]
        sub = int(g.pop("_sum").quantize(one, rounding=ROUND_HALF_UP))
        g["subtotal"] = sub
        total += sub
        out_groups.append(g)
    dates = sorted(p["date"] for p in parsed)
    return {
        "groups": out_groups, "totalTwd": total, "missing": missing,
        "rateTable": [rate_tbl[k] for k in sorted(rate_tbl)],
        "dateFrom": dates[0].isoformat(), "dateTo": dates[-1].isoformat(),
        "categoryLabel": CATEGORIES[category],
        "queriedAt": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": "兆豐國際商業銀行官網－匯率查詢－歷史匯率", "sourceUrl": PAGE + "?tab=3",
    }


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if not isinstance(body, bytes):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = self.path.split("?")[0]
        try:
            if path in ("/", "/index.html"):
                self._send(200, (HERE / "index.html").read_bytes(), "text/html; charset=utf-8")
            elif path == "/api/currencies":
                self._send(200, {"currencies": MEGA.get_currencies(),
                                 "categories": CATEGORIES, "rateTypes": RATE_TYPES})
            else:
                self._send(404, {"error": "not found"})
        except Exception as e:
            self._send(502, {"error": f"連不上兆豐官網：{e}"})

    def do_POST(self):
        if self.path.split("?")[0] != "/api/rates":
            return self._send(404, {"error": "not found"})
        try:
            n = int(self.headers.get("Content-Length") or 0)
            req = json.loads(self.rfile.read(n).decode("utf-8"))
            self._send(200, lookup(req.get("items") or [],
                                   req.get("category", "LastBusinessDayRate")))
        except ValueError as e:
            self._send(400, {"error": str(e)})
        except Exception as e:
            self._send(502, {"error": f"向兆豐官網查詢失敗：{e}"})


def main():
    try:
        srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    except OSError:
        webbrowser.open(f"http://localhost:{PORT}")  # 已經在跑了，直接開頁面
        return
    print(f"出差報支匯率查詢 已啟動： http://localhost:{PORT}   （關閉這個視窗就停止）")
    if "--no-browser" not in sys.argv:
        threading.Timer(0.6, lambda: webbrowser.open(f"http://localhost:{PORT}")).start()
    srv.serve_forever()


if __name__ == "__main__":
    main()
