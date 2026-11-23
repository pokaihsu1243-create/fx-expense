# -*- coding: utf-8 -*-
"""出差報支匯率查詢：兆豐銀行官網「歷史匯率」的即期買入匯率。

網頁版（GitHub Pages）：update_rates.py 每天由 GitHub Actions 執行，把匯率存成 data/ 底下的 JSON，
頁面讀 JSON 自己換算。
本機版：python server.py（或打包後的 exe）→ http://localhost:8728，
data/ 的 JSON 改由本程式即時向兆豐官網查詢後提供，頁面同一份 index.html。
只用標準函式庫。
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
from datetime import date, datetime, timedelta, timezone
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
RATE_TYPE = "Spot"  # 會計算法：即期・銀行買入
TW = timezone(timedelta(hours=8))
FINAL_AFTER = (16, 30)  # 台灣時間過了 16:30，當天的收盤匯率才算定案


def tw_now():
    return datetime.now(TW)


def covered_through(now=None):
    """到哪一天為止的匯率已經定案（過 16:30 才算當天）。"""
    now = now or tw_now()
    d = now.date()
    return d if (now.hour, now.minute) >= FINAL_AFTER else d - timedelta(days=1)


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
            for attempt in range(3):  # 偶爾回空的，換新的 requestId 再問一次（requestId 重複會回空）
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


def fetch_currency(code, start, end, pause=0.0):
    """查一個幣別在期間內三種時點的即期買入匯率 → {類別: {"YYYY-MM-DD": "32.7700"}}"""
    out = {}
    for cat in CATEGORIES:
        tbl = MEGA.query(code, RATE_TYPE, cat, start, end)
        out[cat] = {d.isoformat(): v[0] for d, v in sorted(tbl.items()) if v[0]}
        if pause:
            time.sleep(pause)
    return out


# ---- 本機版：即時提供與網頁版相同格式的 data/*.json ----
_cache = {}
_cache_lock = threading.Lock()


def live_meta():
    cov = covered_through().isoformat()
    return {
        "updatedAt": tw_now().strftime("%Y-%m-%d %H:%M"),
        "mode": "local",
        "categories": CATEGORIES,
        "currencies": [{"code": c["code"], "label": c["label"], "coveredThrough": cov}
                       for c in MEGA.get_currencies()],
    }


def live_rates(code):
    with _cache_lock:
        hit = _cache.get(code)
        if hit and time.time() - hit[0] < 600:
            return hit[1]
    end = tw_now().date()
    data = fetch_currency(code, end - timedelta(days=364), end)
    with _cache_lock:
        _cache[code] = (time.time(), data)
    return data


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
            elif path == "/data/meta.json":
                self._send(200, live_meta())
            elif m := re.fullmatch(r"/data/rates/([A-Z]{3})\.json", path):
                self._send(200, live_rates(m.group(1)))
            else:
                self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(404, {"error": str(e)})
        except Exception as e:
            self._send(502, {"error": f"連不上兆豐官網：{e}"})


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
