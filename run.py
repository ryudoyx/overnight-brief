#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""每交易日 08:40（北京）运行：取数 -> 抓新闻 -> 串讲 -> 出页面 -> 存档。

用法：
  python run.py                 正常跑
  python run.py --no-llm        跳过 Claude，页面照出（新闻只有粗筛结果，没有串讲）
  python run.py --quotes-only   只跑行情，打印事实包，调数据源时用
  python run.py --rebuild DATE  用已存档的事实包重出页面，调样式时用，不重新取数
"""
import os
import sys
import json
import shutil
import datetime as dt

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from brief import quotes, news, narrate, render, metals

ROOT = os.path.dirname(os.path.abspath(__file__))
ARCHIVE = os.path.join(ROOT, "archive")
CST = dt.timezone(dt.timedelta(hours=8))


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def write_outputs(pack):
    """存档一份带日期的，再复制成 index.html 当首页。日期下拉直接读 archive 目录。"""
    day = pack["quotes"]["meta"]["session_date"]
    os.makedirs(ARCHIVE, exist_ok=True)
    with open(os.path.join(ARCHIVE, f"{day}.json"), "w", encoding="utf-8") as f:
        json.dump(pack, f, ensure_ascii=False, indent=1)
    page = render.build(pack)
    dated = os.path.join(ARCHIVE, f"{day}.html")
    open(dated, "w", encoding="utf-8").write(page)
    shutil.copy(dated, os.path.join(ROOT, "index.html"))
    return day, dated


def main():
    args = set(sys.argv[1:])
    cfg = quotes.load_config()

    if "--rebuild" in sys.argv:
        day = sys.argv[sys.argv.index("--rebuild") + 1]
        pack = json.load(open(os.path.join(ARCHIVE, f"{day}.json"), encoding="utf-8"))
        _, dated = write_outputs(pack)
        log(f"已用存档重出页面：{dated}")
        return

    log("取行情")
    q = quotes.collect(cfg)
    if "--quotes-only" in args:
        print(json.dumps(q, ensure_ascii=False, indent=2))
        return

    log("取品种数据（交易所库存 / 沪盘持仓）")
    mt = metals.collect()

    log("抓新闻")
    n = news.collect(cfg) if "--no-llm" not in args else {"meta": {"failures": ["--no-llm 跳过"]}}

    pack = {"quotes": q, "news": n, "metals": mt, "narration": None,
            "commentary": {}, "meta": {}}
    if "--no-llm" not in args:
        log("生成串讲")
        pack["narration"] = narrate.narrate(pack, cfg)
        log("生成品种评论")
        pack["commentary"] = narrate.commentary(pack, cfg)

    # 串讲的花费要按当前后端的单价算：openai_compat 走的是免费额度，
    # 一律按 Claude 单价加会把零成本报成几分钱，页脚不能撒这种谎
    cost = n.get("meta", {}).get("llm_cost_usd", 0) or 0
    price = {"claude-sonnet-5": (3.0, 15.0), "claude-opus-5": (5.0, 25.0),
             "claude-haiku-4-5": (1.0, 5.0)}
    if cfg["judge"]["backend"] == "claude" and (pack.get("narration") or {}).get("usage"):
        u = pack["narration"]["usage"]
        pin, pout = price.get(cfg["judge"]["claude"]["id"], (3.0, 15.0))
        cost += u["input"] / 1e6 * pin + u["output"] / 1e6 * pout
    pack["meta"] = {
        "generated_at": dt.datetime.now(CST).isoformat(timespec="seconds"),
        "cost_usd": round(cost, 4),
    }

    day, dated = write_outputs(pack)
    log(f"完成 {day} → {dated}")


if __name__ == "__main__":
    main()
