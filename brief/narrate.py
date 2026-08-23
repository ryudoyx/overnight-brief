#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""串讲与品种评论——分层结构化输出。

模型返回的是各层字段（宏观主线 / 指数 / 利率汇率 / …），不是一整段文字，
排版交给 render 控制。这样既能保证版面稳定，也让每一层的写作要求可以单独下。

铁律不变：facts 里的数字由代码算好，模型直接引用；news 里的数字来自新闻原文，
可以用；除此之外一个数字都不许出现。

出处规则（2026-08-21 按用户要求定）：
  引用**他人观点**（研报结论、机构判断、官员表态）→ 必须写明来源
  引用**数据**（价格、库存、产量、进出口）→ 不标来源，直接写
"""
import os
import sys
import json
import datetime as dt

import anthropic

CITE_RULE = """
出处规则：
- 引用**他人的观点或判断**时必须点名，写成「国信期货认为…」「美联储官员表示…」
  「加方官员称…」。观点不署名等于把别人的判断冒充成事实。
- 引用**数据**时不要标来源。直接写「7月精炼铜进口 279,557.87 吨，环比下降 16.07%」，
  不要写成「据海关总署，7月…」。数据本身不需要背书，标注只会稀释可读性。

数字规矩：
- facts 里的数字是程序算出来的，直接用。
- news 里的数字是新闻原文写的，可以用。
- **除此之外一个数字都不许出现。** 不要做加减乘除，不要把两条新闻的数字凑成新数字。
- **保持原文的单位和量纲，不要做单位换算。** 原文写「237.85 万吨」就照写，
  不要换成「2378500 吨」。换算是最容易出错的地方（万吨↔吨、亿↔万差一个数量级），
  而且换算之后读的人没法拿它跟原文核对。
- 没有的数据就不写那句话。特别是加工费 TC、社会库存、运行产能、周产量、开工率、
  即时利润、成本线——这些我没给你，绝对不要编。
- 不要写价格预测区间。你没有这个信息。
- 数字一律用阿拉伯数字，不要写成中文大写。

**某一层没有实质内容时，直接写短。** 字数区间是上限不是配额——如果需求端
当晚确实没有新信息，就写一句「消息面无新增需求侧信息」，不要用「市场正密切
观察政策传导」「消费信号处于震荡调整阶段」这类句子把字数填满。空话比留白更糟，
它让读的人误以为读到了内容。

**不要为价格变动编造原因。** 只有当 news 里明确写出了原因，才可以写因果。
如果某只股票大跌而消息面没有给出解释，就只陈述现象（「Moderna 跌 23.55%
拖累医疗板块」），不要补一个「因财报不及预期」——你不知道，猜的原因会被当成事实。
宁可写「消息面未给出解释」，也不要编一个听起来合理的理由。
"""

NARRATION_SYSTEM = """你在给一位做铜和有色为主的大宗商品研究员写每日隔夜早报的开篇。
他早上 8 点打开网页，先看到你写的这几层，然后才是数据表格。

按下面七个字段分别作答，每一层各司其职，不要互相复述：

tone      12 字以内的定调，例如「财政与通胀双重定价，股债双杀」
macro     **最重要的一层**。不要复述价格，要回答「昨夜发生了什么、它如何解释这些价格」。
          把跨资产的矛盾点显性化——比如股票和债券同时下跌而黄金大涨，这说明资金
          交易的是财政与通胀，而不是传统避险。有几条消息指向同一个方向就串起来讲。
          150-250 字。
indices   指数与波动率。除了涨跌幅，说清楚结构特征（大盘跌得多还是小盘跌得多，
          这通常能指示压力来自利率还是来自单一行业）。60 字以内。
rates_fx  利率与汇率。重点是它跟股票、美元、商品之间是否自洽。60 字以内。
sectors   板块。领涨领跌各说几个，并判断这是结构性调仓还是系统性抛售。60 字以内。
movers    个股异动。**不要只罗列涨跌幅**，挑最值得说的一两只，并尽量与当晚的宏观
          消息交叉印证（例如零售龙头大跌 × 住房可负担性数据恶化 = 消费端压力）。
          80-120 字。
commodities 商品。60 字以内。
""" + CITE_RULE

COMMENT_SYSTEM = """你在给一位做铜和有色为主的大宗商品研究员写每日品种评论，他早上 8 点看。
体例参照卖方周报的品种页：分五段，每段只讲自己那一层的事，不要互相复述。

tone       12 字以内的定调，例如「挤仓退潮，近端缺乏驱动」

market     **盘面**。内外盘价格与涨跌幅、持仓及其变化、现货升贴水或基差、
           相关股票表现。这一段是「现在是什么价位、什么结构」。80-140 字。
           **不要在这一段写库存**——库存有专属的一段，写在这里会让那一段没得写。

supply     **供应**。矿端扰动（罢工/停产/检修/复产）、加工费 TC/RC 的方向、
           冶炼开工与检修、进口货源与到港。80-140 字。

demand     **需求**。下游开工与补库、终端行业（电网、地产、汽车、光伏、家电、
           数据中心）的排产与出口、消费旺淡季位置。80-140 字。

inventory  **库存**。这一段必须写，不能留空。交易所库存（上期所/LME/COMEX）的
           单日与多日变化、注册与注销仓单、社会库存与保税区方向。库存要注意
           时间尺度——单日变化和五日累计方向相反时，这本身就是最重要的信息，
           必须点破。facts 里给了交易所库存的 stock / change / change_5d，
           至少要把这三个数写清楚。80-140 字。

conclusion **结论**。当前主导矛盾是什么，指向哪个方向。60-100 字。

           **不要以「建议关注…」「需注意…」「留意后续…」结尾。** 这类句子看似
           谨慎，实际不传递任何信息——读的人本来就知道要关注市场。

           要写风险，就写成**可证伪的条件**：说清楚「什么情况出现，上面的判断就
           不成立」。比如不要写「建议关注库存能否持续去化」这种废话，而要写成
           「若五日累库转为持续去库，近端压制解除」这种能拿去验证的判断。

           **注意你写的是哪个品种。** 上面的例子只是示范句式，不要把例子里的
           品种名照抄进来——写铝就说铝价，写铜就说铜价，别串。

           如果存在明显背离（价格不动而股票大涨、外盘跌而内盘扛），在这里点破。

**写数字的体例**：只要手上有变化量，就把水平值和变化量一起写，写成
「上期所库存 53,079 吨（单日 -3,619，五日 +25,871）」这样，而不是只写一个孤零零
的水平值。没有变化量的就只写水平值，不要为了凑格式去算。

**某一层没有实质内容时，直接写短。** 字数区间是上限不是配额——如果需求端当晚
确实没有新信息，就写一句「消息面无新增需求侧信息」，不要用「市场正密切观察政策
传导」这类句子把字数填满。空话比留白更糟，它让读的人误以为读到了内容。
""" + CITE_RULE

NARRATION_FIELDS = ["tone", "macro", "indices", "rates_fx", "sectors", "movers", "commodities"]
COMMENT_FIELDS = ["tone", "market", "supply", "demand", "inventory", "conclusion"]


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def _schema(fields):
    return {"type": "object",
            "properties": {f: {"type": "string"} for f in fields},
            "required": fields, "additionalProperties": False}


def json_call(system, user, cfg, schema, hint="", max_tokens=4000):
    """按当前后端发一次结构化请求，返回解析后的 JSON；失败返回 None。"""
    backend = cfg["judge"]["backend"]
    try:
        if backend == "openai_compat":
            from .compat import CompatJudge
            raw = CompatJudge(cfg["judge"]["openai_compat"]).chat(
                system + hint, user, max_tokens=max_tokens)
        else:
            client = anthropic.Anthropic()
            resp = client.messages.create(
                model=os.environ.get("BRIEF_MODEL") or cfg["judge"]["claude"]["id"],
                max_tokens=max_tokens, system=system,
                output_config={"effort": cfg["judge"]["claude"]["effort"],
                               "format": {"type": "json_schema", "schema": schema}},
                messages=[{"role": "user", "content": user}])
            if resp.stop_reason == "refusal":
                return None
            raw = next((b.text for b in resp.content if b.type == "text"), "")
        return json.loads(raw)
    except Exception as e:
        log(f"  生成失败：{type(e).__name__}: {str(e)[:110]}")
        return None


def _one_shot(system, user, cfg, fields, max_tokens=4000):
    """结构化多字段输出；缺字段补空串，别让某层缺失把整段丢掉。"""
    hint = "\n\n只输出 JSON，不要 markdown 代码块，不要解释文字。字段：" + "、".join(fields)
    got = json_call(system, user, cfg, _schema(fields), hint, max_tokens)
    if got is None:
        return None
    return {f: str(got.get(f, "") or "").strip() for f in fields}


def _brief_facts(pack):
    """喂给模型的精简事实包。不放 spark 序列和 url——模型不需要，
    只会诱导它对着一串数字做算术。"""
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
        "宏观新闻": [{k: r[k] for k in ("title", "summary", "direction", "importance")}
                     for r in n.get("宏观", [])],
    }


def narrate(pack, cfg):
    if cfg["judge"]["backend"] == "rules":
        log("规则档不生成串讲（要这几层就换 openai_compat 或 claude）")
        return None
    got = _one_shot(
        NARRATION_SYSTEM,
        "昨夜的事实如下（数字均已由程序算好，直接引用即可）：\n\n"
        + json.dumps(_brief_facts(pack), ensure_ascii=False, indent=1),
        cfg, NARRATION_FIELDS)
    if got:
        log(f"串讲已生成（宏观主线 {len(got['macro'])} 字）")
    return got


def _variety_facts(pack, variety):
    q = pack["quotes"]
    m = (pack.get("metals") or {}).get("varieties", {})
    outer = {"铜": "COMEX 铜", "铝": None}[variety]

    facts = {"品种": variety}
    if m.get(variety):
        facts["上期所交易所库存"] = m[variety].get("inventory")
        facts["沪盘"] = m[variety].get("quote")
    if variety == "铝" and m.get("氧化铝"):
        facts["氧化铝交易所库存"] = m["氧化铝"].get("inventory")
        facts["氧化铝盘面"] = m["氧化铝"].get("quote")
    if outer:
        facts["外盘"] = next((r for r in q["commodities"] if r["name"] == outer), None)
    if variety == "铜":
        facts["铜矿股隔夜"] = [{"name": r["name"], "chg_pct": r["chg_pct"]}
                               for r in q["copper"]["miners"]]
    facts["美股大盘"] = [{"name": r["name"], "chg_pct": r["chg_pct"]}
                         for r in q["indices"] if r["name"] in ("标普500", "VIX 恐慌指数")]
    facts["美元指数"] = next((r["chg_pct"] for r in q["macro_markets"]
                              if r["name"] == "美元指数"), None)
    return facts


def commentary(pack, cfg):
    if cfg["judge"]["backend"] == "rules":
        log("规则档不生成品种评论")
        return {}
    weekend = (pack["news"].get("meta") or {}).get("weekend")
    out = {}
    for variety in ("铜", "铝"):
        facts = _variety_facts(pack, variety)
        news = pack["news"].get(variety, [])
        if not facts.get("沪盘") and not news:
            log(f"{variety}：既无盘面也无消息，跳过评论")
            continue
        # 周末消息稀疏时不硬写评论：只有盘面没有消息面，写出来的只能是复述价格，
        # 不如把版面留给宏观和 AI
        if weekend and len(news) < 2:
            log(f"{variety}：周末且仅 {len(news)} 条消息，跳过评论")
            continue
        payload = ("facts（程序算好的，直接引用）：\n"
                   + json.dumps(facts, ensure_ascii=False, indent=1)
                   + "\n\nnews（新闻原文；里面的观点要点名，数据不用标）：\n"
                   + json.dumps([{k: n[k] for k in ("title", "summary", "direction", "source")}
                                 for n in news], ensure_ascii=False, indent=1))
        got = _one_shot(COMMENT_SYSTEM, payload, cfg, COMMENT_FIELDS)
        if got:
            out[variety] = got
            log(f"{variety}：评论已生成")
    return out
