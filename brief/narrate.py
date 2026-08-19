#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""把算好的事实包交给 Claude 写一段中文串讲。

铁律：模型收到的每一个数字都是 quotes.py 实测出来的，它只负责组织和取舍。
系统提示里明确禁止它自己算涨跌幅、算幅度、算相关性——那些一旦编出来，
整份早报的可信度就没了。
"""
import os
import sys
import json
import datetime as dt

import anthropic

SYSTEM = """你在给一位做铜为主的大宗商品研究员写每日隔夜早报的开篇串讲。他早上 9 点打开网页，
这段话是他看到的第一段文字，后面才是数据表格。

写作要求：
- 150-250 字，中文，一段到两段，不要小标题不要 bullet
- 先说昨夜的主线是什么，再说值得注意的分歧或异常
- 只用我给你的数字。**绝对不要自己计算任何数值**——不要算涨跌幅、不要算比值、
  不要把两个数相减，不要说"累计""较上周"这种需要额外数据才能成立的话。
  你手上没有的数字就不要提。
- 不要复述整张表。挑两三件真正重要的说透，其余交给表格
- 不要给交易建议，不要预测明天
- 不要用"值得关注""需要警惕"这类正确的废话；说清楚事实和它的含义就够了
- 如果数据里有背离（比如铜价跌但铜矿股涨），那通常就是最值得讲的一件事

另外给一个 tone：一句话概括昨夜定调，12 字以内，会显示在页面最顶部。
例如「风险偏好回落，商品分化」「铜逼仓主导，美股缩量」。

按这个 JSON 返回，不要有别的内容：
{"tone": "...", "text": "..."}"""


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def _brief_facts(pack):
    """喂给模型的精简版事实包。

    刻意不把 spark 序列和 url 塞进去——模型不需要，只会诱导它对着一串数字做算术。
    """
    q, n = pack["quotes"], pack["news"]

    def rows(items, keys=("name", "close", "chg_pct")):
        return [{k: r.get(k) for k in keys} for r in items]

    return {
        "美股所属交易日": q["meta"]["session_date"],
        "指数": rows(q["indices"]),
        "利率汇率": rows(q["macro_markets"]),
        "板块涨跌": [{"name": r["name"], "chg_pct": r["chg_pct"]} for r in q["sectors"]],
        "个股异动_涨": rows(q["movers"]["up"], ("sym", "name", "chg_pct")),
        "个股异动_跌": rows(q["movers"]["down"], ("sym", "name", "chg_pct")),
        "商品": [{"name": r["name"], "close": r["close"], "unit": r["unit"],
                  "chg_pct": r["chg_pct"]} for r in q["commodities"]],
        "自选股": [{"group": r["group"], "name": r["name"], "chg_pct": r["chg_pct"]}
                   for r in q["watchlist"]],
        "铜矿股": [{"name": r["name"], "chg_pct": r["chg_pct"]} for r in q["copper"]["miners"]],
        "沪铜夜盘": q["copper"]["shfe_night"],
        "宏观新闻": [{k: r[k] for k in ("title", "summary", "direction", "importance")}
                     for r in n.get("宏观", [])],
        "铜新闻": [{k: r[k] for k in ("title", "summary", "direction", "importance")}
                   for r in n.get("铜", [])],
    }


def narrate(pack, cfg):
    """规则档没有叙述能力，直接跳过——页面照出，只是少这一段。"""
    backend = cfg["judge"]["backend"]
    if backend == "rules":
        log("规则档不生成串讲（要这段话就把 judge.backend 换成 openai_compat 或 claude）")
        return None
    if backend == "openai_compat":
        from .compat import CompatJudge
        j = CompatJudge(cfg["judge"]["openai_compat"])
        try:
            text = j.chat(SYSTEM, "昨夜的事实如下（所有数字均已由程序算好，直接引用即可）：\n\n"
                          + json.dumps(_brief_facts(pack), ensure_ascii=False, indent=1),
                          max_tokens=2000)
            out = json.loads(text)
        except Exception as e:
            log(f"串讲生成失败（页面照常出）：{type(e).__name__}: {str(e)[:120]}")
            return None
        out["usage"] = j.usage
        return out

    model = os.environ.get("BRIEF_MODEL") or cfg["judge"]["claude"]["id"]
    client = anthropic.Anthropic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=4000,
            system=SYSTEM,
            output_config={
                "effort": cfg["judge"]["claude"]["effort"],
                "format": {"type": "json_schema", "schema": {
                    "type": "object",
                    "properties": {"tone": {"type": "string"}, "text": {"type": "string"}},
                    "required": ["tone", "text"],
                    "additionalProperties": False,
                }},
            },
            messages=[{
                "role": "user",
                "content": "昨夜的事实如下（所有数字均已由程序算好，直接引用即可）：\n\n"
                           + json.dumps(_brief_facts(pack), ensure_ascii=False, indent=1),
            }],
        )
    except (anthropic.APIStatusError, anthropic.APIConnectionError) as e:
        log(f"串讲生成失败（页面照常出，只是少这一段）：{type(e).__name__}: {str(e)[:120]}")
        return None

    if resp.stop_reason == "refusal":
        log("模型拒答串讲，跳过")
        return None
    text = next((b.text for b in resp.content if b.type == "text"), "")
    try:
        out = json.loads(text)
    except json.JSONDecodeError:
        log("串讲输出不是合法 JSON，跳过")
        return None
    out["usage"] = {"input": resp.usage.input_tokens, "output": resp.usage.output_tokens}
    return out
