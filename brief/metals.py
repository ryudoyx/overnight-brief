#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""铜、铝的品种数据层——给品种评论提供可核对的事实。

只放能从免费源拿到、且能自己算准的数字。拿不到的字段一律留空并在
`gaps` 里点名，让下游明确知道哪几项没有数据，而不是让模型去猜。

口径说明：
- 交易所库存走东财（日更），不是 SMM 社会库存——两者不是一回事，别混用
- 盘面取新浪连续合约。早 8 点跑时国内还没开盘，拿到的是昨日日盘 + 昨夜夜盘
- LME 库存 akshare 那个接口已停更一个多月，这里不取；LME 数字只能从
  SHMET 快讯原文里引用并标注出处
"""
import sys
import datetime as dt

ROOT_VARIETIES = {
    "铜": {"inv": "沪铜", "quote": "沪铜", "unit": "吨"},
    "铝": {"inv": "沪铝", "quote": "沪铝", "unit": "吨"},
    "氧化铝": {"inv": "氧化铝", "quote": "氧化铝", "unit": "吨"},
}


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def _num(v, nd=2):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return round(f, nd)


def exchange_inventory(name):
    """上期所交易所库存 + 日增减。注意这是交易所库存，不是社会库存。"""
    import akshare as ak
    df = ak.futures_inventory_em(symbol=name)
    if df is None or df.empty:
        return None
    last = df.iloc[-1]
    prev5 = df.iloc[-6] if len(df) >= 6 else df.iloc[0]
    return {
        "date": str(last["日期"]),
        "stock": int(last["库存"]),
        "change": _num(last["增减"], 0),
        "change_5d": int(last["库存"]) - int(prev5["库存"]),
        "source": "东财/上期所交易所库存",
    }


def shfe_quote(name):
    """新浪连续合约盘面：价格、涨跌幅、成交、持仓。"""
    import akshare as ak
    df = ak.futures_zh_realtime(symbol=name)
    if df is None or df.empty:
        return None
    r = df.iloc[0]
    return {
        "symbol": str(r["symbol"]),
        "close": _num(r["trade"], 0),
        "chg_pct": _num(float(r["changepercent"]) * 100 if abs(float(r["changepercent"])) < 1
                        else float(r["changepercent"])),
        "prev_settle": _num(r["prevsettlement"], 0),
        "volume": int(r["volume"]) if r["volume"] else None,
        "open_interest": int(r["position"]) if r["position"] else None,
        "trade_date": str(r["tradedate"]),
        "unit": "CNY/t",
        "source": "新浪连续合约",
    }


def collect():
    """返回 {品种: {inventory, quote}} + gaps（点名缺哪些数据）。"""
    out, gaps = {}, []
    for cn, spec in ROOT_VARIETIES.items():
        item = {}
        for label, fn, key in (("库存", exchange_inventory, "inv"),
                               ("盘面", shfe_quote, "quote")):
            try:
                item[key if key == "quote" else "inventory"] = fn(spec[key if key == "quote" else "inv"])
            except Exception as e:
                item[key if key == "quote" else "inventory"] = None
                log(f"  {cn}{label}取数失败：{type(e).__name__}: {str(e)[:60]}")
        out[cn] = item

    # 明确点名拿不到的字段，下游据此在页面上标注「无数据」，
    # 而不是让模型看着空缺自由发挥
    # 只列「结构化数值仍拿不到」的字段。TC、现货升水、社库这些，Mysteel 日报和
    # SHMET/SMM 快讯的标题里常有方向性描述（如「铜精矿TC低位承压」「现货升水
    # 持续承压走跌」），评论可以引用；但没有可入库的数值序列。
    gaps = [
        "精确数值：进口铜精矿 TC、现货升贴水、社会库存（新闻里有方向，无数值序列）",
        "电解铝、氧化铝运行产能与周产量（SMM/阿拉丁订阅）",
        "型材开工率、下游开工率（SMM 订阅）",
        "电解铝即时利润、氧化铝即期成本（需成本模型 + 订阅原料价）",
        "LME 库存与升贴水（akshare 接口已停更，只能引用快讯原文）",
    ]
    return {"varieties": out, "gaps": gaps}


if __name__ == "__main__":
    import json
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
