#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""隔夜行情取数：指数 / 板块 / 个股异动 / 商品 / 铜，输出一个「事实包」dict。

这里算出的每一个数字都是最终稿——LLM 后面只负责把它们串成话，不许自己算。

两个必须钉死的坑：
  1. Yahoo 的 v8/chart 用普通 requests 直接 429，必须走 curl_cffi 指纹伪装。
     yfinance 内部已经这么做了，所以取数一律走 yfinance，不手搓 HTTP。
  2. HG=F 返回的是 USD/lb（不是美分/磅，也不是美元/吨）。换算因子写死在
     CONVERT 里，任何新增商品都要先实测 meta.currency 再填。
"""
import os
import sys
import math
import datetime as dt
from concurrent.futures import ThreadPoolExecutor

import yaml
import yfinance as yf
from curl_cffi import requests as cr

# 从模块自身位置推导项目根，别写死家目录——
# GitHub Actions 的 runner 上没有 ~/Desktop，写死了云端必挂
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")

# 落库口径换算：(raw_unit, unit_to) -> 乘数
CONVERT = {
    ("USD/lb", "USD/t"): 2204.62262,
    ("USD/oz", "USD/oz"): 1.0,
    ("USD/bbl", "USD/bbl"): 1.0,
    ("USD/MMBtu", "USD/MMBtu"): 1.0,
}


def log(msg):
    # 走 stderr，好让 stdout 保持成一份干净的 JSON，方便 `python -m brief.quotes > facts.json`
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def _num(v, nd=2):
    """NaN / inf 一律变 None，别让脏数字漏进事实包。"""
    if v is None:
        return None
    try:
        v = float(v)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{sym}?range={rng}&interval={itv}"

_SESS = None


def session():
    """必须伪装浏览器指纹：普通 requests 打这个接口稳定 429。"""
    global _SESS
    if _SESS is None:
        _SESS = cr.Session(impersonate="chrome")
    return _SESS


def _bars_of(res):
    """把 chart 响应摊平成 [(交易日, close 或 None, volume)]。

    刻意保留 close 为 None 的行——哪一天有交易但还没结算，是判断「前收在哪」的关键信息，
    提前 dropna 会让缺口悄无声息地消失。
    """
    meta = res["meta"]
    off = dt.timezone(dt.timedelta(seconds=meta.get("gmtoffset", 0)))
    q = res["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(res.get("timestamp") or []):
        d = dt.datetime.fromtimestamp(t, dt.timezone.utc).astimezone(off)
        c = q["close"][i]
        bars.append((d.date(), float(c) if c is not None else None, q["volume"][i]))
    return bars, off


def fetch_chart(sym, rng="3mo", itv="1d", tries=3):
    for i in range(tries):
        try:
            r = session().get(CHART.format(sym=sym, rng=rng, itv=itv), timeout=20)
            if r.status_code != 200:
                raise RuntimeError(f"HTTP {r.status_code}")
            res = r.json()["chart"]["result"][0]
            bars, off = _bars_of(res)
            return bars, res["meta"], off
        except Exception as e:
            if i == tries - 1:
                log(f"  {sym} 取数失败：{type(e).__name__}: {str(e)[:70]}")
                return None
            import time
            time.sleep(1.5 * (i + 1))


def resolve_close(sym, day):
    """日线还没结算那一天的收盘价，用小时线最后一根补。

    实测 ^GSPC 用这个补出来的值跟 meta.regularMarketPrice 完全一致，可信。
    """
    got = fetch_chart(sym, rng="5d", itv="1h")
    if not got:
        return None
    bars, _, _ = got
    same = [c for d, c, _ in bars if d == day and c is not None]
    return same[-1] if same else None


def fetch_all(symbols):
    """并发拉全部标的。并发压到 6，Yahoo 对突发请求很敏感。"""
    log(f"取日线 {len(symbols)} 个标的")
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(fetch_chart, symbols))
    out = {}
    for sym, r in zip(symbols, results):
        if r and len([b for b in r[0] if b[1] is not None]) >= 2:
            out[sym] = r
    missing = [s for s in symbols if s not in out]
    if missing:
        log(f"  取不到：{', '.join(missing)}")
    return out


def snapshot(sym, payload, spark_days, mult=1.0):
    """切出「最近一场已收盘交易日」的快照。

    北京时间 8:40 跑的时候有两件事同时成立，缺一个都会算错：
      1. 最近一根日线 bar 的 close 还是 null——盘早收了，但 Yahoo 的日线聚合要再等几小时。
         真收盘价只在 meta.regularMarketPrice 里，所以现价一律取 meta。
      2. 各标的的 regularMarketTime 落在哪一天并不一致：标普停在 8/17 收盘，
         而 VIX、期货这类有夜盘的已经跳到 8/18 凌晨。所以「当前是哪一场」必须逐个判断，
         再取严格早于它的那一场当前收。
    如果前一场的日线也还没结算（VIX 就是这样），就掉头用小时线把它补回来——
    不补的话会跨过一整天，把两天的涨幅当成隔夜涨幅。
    """
    bars, meta, off = payload
    last = float(meta["regularMarketPrice"])
    mkt_time = dt.datetime.fromtimestamp(meta["regularMarketTime"], dt.timezone.utc).astimezone(off)
    cur_day = mkt_time.date()

    prior = [b for b in bars if b[0] < cur_day]
    if not prior:
        return None
    prev_day, prev_raw, _ = prior[-1]
    if prev_raw is None:
        prev_raw = resolve_close(sym, prev_day)
        if prev_raw is None:                      # 补不回来就退到再前一根，并如实记下跨了几天
            done = [b for b in prior if b[1] is not None]
            if not done:
                return None
            prev_day, prev_raw = done[-1][0], done[-1][1]
        log(f"  {sym} 前收 {prev_day} 日线未结算，已用小时线补回")

    close, prev_close = last * mult, prev_raw * mult
    span = (cur_day - prev_day).days

    vol = meta.get("regularMarketVolume")
    base = [v for d, c, v in prior if v]
    base = base[-20:]
    vol_ratio = vol / (sum(base) / len(base)) if vol and len(base) >= 5 else None

    hist = [c * mult for d, c, _ in prior if c is not None]
    spark = (hist + [close])[-spark_days:]

    nd = 4 if abs(close) < 10 else 2
    return {
        "close": _num(close, nd),
        "prev_close": _num(prev_close, nd),
        "chg": _num(close - prev_close, nd),
        "chg_pct": _num((close / prev_close - 1) * 100 if prev_close else None),
        "session_date": cur_day.isoformat(),
        "prev_session_date": prev_day.isoformat(),
        "quote_at": mkt_time.isoformat(timespec="minutes"),
        "span_days": span,                        # >4 说明跨过了缺口，渲染时别硬说成「隔夜」
        "volume": int(vol) if vol else None,
        "vol_ratio": _num(vol_ratio),
        "spark": [_num(v, 4) for v in spark],
    }


def build_quotes(cfg):
    spark_days = cfg["runtime"]["spark_days"]

    groups = {
        "indices": cfg["indices"],
        "macro_markets": cfg["macro_markets"],
        "sectors": cfg["sectors"],
        "commodities": cfg["commodities"],
    }
    watch = [dict(g, group=name) for name, lst in cfg["watchlist"].items() for g in lst]

    symbols = sorted({e["sym"] for lst in groups.values() for e in lst} | {e["sym"] for e in watch})
    hist = fetch_all(symbols)

    facts = {}
    for name, entries in groups.items():
        rows = []
        for e in entries:
            df = hist.get(e["sym"])
            if df is None:
                continue
            mult = 1.0
            if name == "commodities":
                key = (e["raw_unit"], e["unit_to"])
                if key not in CONVERT:
                    raise KeyError(f"{e['sym']} 缺换算因子 {key}，先实测口径再加进 CONVERT")
                mult = CONVERT[key]
            snap = snapshot(e["sym"], df, spark_days, mult)
            if snap is None:
                continue
            row = {"sym": e["sym"], "name": e["name"], **snap}
            if name == "commodities":
                row["unit"] = e["unit_to"]
            if e.get("extra"):
                row["extra"] = True
            rows.append(row)
        facts[name] = rows

    rows = []
    for e in watch:
        if e["sym"] not in hist:
            continue
        snap = snapshot(e["sym"], hist[e["sym"]], spark_days)
        if snap:
            rows.append({"sym": e["sym"], "name": e["name"], "group": e["group"], **snap})
    facts["watchlist"] = rows

    # 板块按涨跌幅排序，方便渲染时直接画条形图
    facts["sectors"].sort(key=lambda r: (r["chg_pct"] is None, -(r["chg_pct"] or 0)))
    return facts


def fetch_movers(cfg):
    """全美股大票异动榜。走 Yahoo screener，一次两个请求就够，不用扫 500 只。"""
    m = cfg["movers"]
    q = yf.EquityQuery("and", [
        yf.EquityQuery("eq", ["region", "us"]),
        yf.EquityQuery("gt", ["intradaymarketcap", m["min_market_cap"]]),
        yf.EquityQuery("gt", ["dayvolume", m["min_volume"]]),
    ])

    def pull(asc):
        res = yf.screen(q, sortField="percentchange", sortAsc=asc, size=m["top_n"] * 3)
        rows = []
        for x in res.get("quotes", []):
            pct = x.get("regularMarketChangePercent")
            if pct is None or abs(pct) < m["min_abs_pct"]:
                continue
            rows.append({
                "sym": x.get("symbol"),
                "name": x.get("shortName"),
                "close": _num(x.get("regularMarketPrice")),
                "chg_pct": _num(pct),
                "market_cap": x.get("marketCap"),
                "volume": x.get("regularMarketVolume"),
            })
            if len(rows) >= m["top_n"]:
                break
        return rows

    log("取个股异动榜")
    return {"up": pull(False), "down": pull(True)}


def fetch_shfe_copper(cfg):
    """沪铜夜盘收盘。取不到就返回 None——COMEX 才是隔夜主场，这个是锦上添花。"""
    try:
        import akshare as ak
        import pandas as pd
        df = ak.futures_zh_minute_sina(symbol=cfg["copper"]["shfe_symbol"], period="15")
        df["datetime"] = pd.to_datetime(df["datetime"])
        night = df[df["datetime"].dt.hour.isin([21, 22, 23, 0, 1])]
        if night.empty:
            return None
        last = night.iloc[-1]
        return {
            "sym": cfg["copper"]["shfe_symbol"],
            "name": "沪铜连续（夜盘）",
            "close": _num(last["close"], 0),
            "unit": "CNY/t",
            "at": str(last["datetime"]),
        }
    except Exception as e:
        log(f"  沪铜夜盘取数失败（不致命）：{type(e).__name__}: {str(e)[:80]}")
        return None


def collect(cfg=None):
    cfg = cfg or load_config()
    facts = build_quotes(cfg)
    facts["movers"] = fetch_movers(cfg)

    miners = {r["sym"] for r in cfg["copper"]["miners"]} if isinstance(cfg["copper"]["miners"][0], dict) \
        else set(cfg["copper"]["miners"])
    facts["copper"] = {
        "comex": next((r for r in facts["commodities"] if r["sym"] == "HG=F"), None),
        "shfe_night": fetch_shfe_copper(cfg),
        "miners": [r for r in facts["watchlist"] if r["sym"] in miners],
    }

    # 早报口径的「昨夜」= 美股正股那一场。VIX、期货有夜盘，它们的 session_date 会比这个晚一天，
    # 属正常，不要拿它们去定全局日期。
    eq_dates = [r["session_date"] for r in facts["sectors"] + facts["watchlist"] if r.get("session_date")]
    facts["meta"] = {
        "generated_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "session_date": max(set(eq_dates), key=eq_dates.count) if eq_dates else None,
    }
    return facts


if __name__ == "__main__":
    import json
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
