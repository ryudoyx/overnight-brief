#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""纯规则打分——零 key、零成本、零外部依赖。

打分骨架整套搬自 copper-watch（同组命中收益递减、硬事件兜底星级、带数字加权、
来源权重），实测在铜上跑了一段时间，信号词覆盖得住。这里的新增量是一份宏观词表。

它唯一做不到的是把英文标题翻成中文——输出结构跟 LLM 后端完全一致，
render 那边不用区别对待。
"""
import re

# 每条: (权重, 方向, 分类, [关键词...])
#   方向 +1 = 利多, -1 = 利空, 0 = 判不了(中性), None = 反向指标(看涨跌定方向)
#   权重 越大越像"值得马上看一眼"的硬事件

COPPER_SIGNALS = [
    (3.0, +1, "供应", [
        "strike", "walkout", "halt", "halted", "suspend", "suspended", "suspension",
        "force majeure", "shutdown", "shut down", "disruption", "outage",
        "accident", "landslide", "collapse", "flooding", "blockade", "protest",
        "derailment", "seismic", "earthquake", "wildfire",
        "罢工", "停产", "停工", "暂停", "中断", "事故", "坍塌", "透水",
        "滑坡", "封锁", "抗议", "不可抗力", "地震", "火灾",
    ]),
    (2.5, +1, "供应", [
        "output cut", "production cut", "cuts output", "cut production",
        "curtail", "curtailment", "lower guidance", "cuts guidance",
        "trims forecast", "reduce output",
        "减产", "限产", "下调产量", "产量下调", "指引下调", "检修", "停槽",
    ]),
    (2.0, -1, "供应", [
        "ramp up", "ramp-up", "first production", "commissioning", "commissioned",
        "restart", "resumes", "resumed", "expansion approved", "raises guidance",
        "boost output", "increase output", "new mine",
        "投产", "复产", "达产", "爬产", "扩建", "增产", "复工", "上调产量",
    ]),
    (3.0, None, "供应", [
        "treatment charge", "tc/rc", "treatment and refining",
        "smelter maintenance", "smelter cut", "smelter halt",
        "加工费", "粗炼费", "精炼费", "冶炼厂检修", "冶炼减产",
    ]),
    (2.0, None, "库存", [
        "inventories", "inventory", "stockpiles", "stocks fell", "stocks rose",
        "lme warehouse", "cancelled warrants", "bonded",
        "库存", "仓单", "保税区", "社会库存", "显性库存",
    ]),
    # 关税权重压在硬事件线以下：它是话题词不是事件词，热议期满屏都是。
    # 真落地的政策会同时命中多组词，总分自然上得去。
    (2.2, 0, "宏观政策", [
        "tariff", "export ban", "export restriction", "quota", "sanction",
        "section 232", "nationalization", "royalty", "windfall tax",
        "关税", "出口禁令", "出口限制", "配额", "制裁", "国有化", "矿业税",
    ]),
    (2.6, +1, "价格交易", [
        "squeeze", "backwardation", "record high", "all-time high",
        "逼仓", "挤仓", "现货升水", "历史新高",
    ]),
    (2.0, +1, "宏观政策", [
        "deficit", "shortage", "tight supply", "supply squeeze", "undersupply",
        "短缺", "缺口", "供应紧张", "偏紧",
    ]),
    (2.0, -1, "宏观政策", [
        "surplus", "oversupply", "glut", "ample supply",
        "过剩", "宽松", "供应充裕",
    ]),
    (2.0, 0, "需求", [
        "grid investment", "power grid", "ev sales", "solar installation",
        "demand growth", "demand weak", "orders",
        "电网投资", "新能源车", "光伏装机", "空调排产", "开工率", "订单", "需求",
    ]),
    (1.5, 0, "公司项目", [
        "guidance", "quarterly output", "annual production", "acquisition",
        "merger", "feasibility study", "resource estimate", "permit",
        "产量指引", "季度产量", "年产", "收购", "并购", "可研", "资源量", "许可",
    ]),
    (1.0, 0, "价格交易", [
        "contango", "premium", "spread", "arbitrage",
        "升贴水", "价差", "套利", "基差",
    ]),
]

# 宏观档：方向一律指「对风险资产（美股）」的影响
MACRO_SIGNALS = [
    (3.0, +1, "货币政策", [
        "rate cut", "cuts rates", "cut rates", "dovish", "easing", "pivot",
        "降息", "宽松", "鸽派", "转向",
    ]),
    (3.0, -1, "货币政策", [
        "rate hike", "hikes rates", "raise rates", "hawkish", "tightening",
        "加息", "紧缩", "鹰派",
    ]),
    (2.6, 0, "货币政策", [
        "fomc", "federal reserve", "powell", "fed minutes", "dot plot",
        "ecb", "bank of japan", "boj", "pboc", "central bank",
        "美联储", "议息", "点阵图", "欧洲央行", "日本央行", "央行",
    ]),
    (2.8, 0, "经济数据", [
        "cpi", "ppi", "pce", "inflation", "nonfarm", "payrolls", "jobless claims",
        "unemployment rate", "retail sales", "ism", "pmi", "gdp",
        "consumer confidence", "durable goods",
        "通胀", "非农", "失业率", "初请", "零售销售", "制造业指数", "经济增速",
    ]),
    # 数据超/不及预期才定方向，单独一组，跟上面的"有数据公布"叠加
    (2.0, -1, "经济数据", [
        "hotter than expected", "above expectations", "beats expectations",
        "accelerated", "hot inflation",
        "超预期", "高于预期", "反弹",
    ]),
    (2.0, +1, "经济数据", [
        "cooler than expected", "below expectations", "misses expectations",
        "slowed", "eased",
        "不及预期", "低于预期", "降温", "放缓",
    ]),
    (2.8, -1, "关税与贸易", [
        "tariff", "new tariffs", "trade war", "export controls", "sanction",
        "section 232", "trade restriction",
        "关税", "贸易战", "出口管制", "制裁",
    ]),
    (2.4, +1, "关税与贸易", [
        "trade deal", "trade agreement", "tariff exemption", "tariff pause",
        "lifts tariffs", "suspends tariffs",
        "贸易协定", "关税豁免", "暂停关税", "取消关税",
    ]),
    (3.0, -1, "地缘与能源", [
        "missile", "airstrike", "invasion", "attack", "conflict escalat",
        "war", "military strike", "drone attack",
        "导弹", "空袭", "袭击", "冲突升级", "开战", "军事打击",
    ]),
    (2.4, 0, "地缘与能源", [
        "opec", "opec+", "oil output", "crude supply", "production quota",
        "strait of hormuz", "pipeline",
        "欧佩克", "原油产量", "减产协议", "霍尔木兹", "管道",
    ]),
    (2.6, -1, "财政与政治", [
        "government shutdown", "debt ceiling", "default", "downgrade",
        "credit rating cut", "impeach",
        "政府停摆", "债务上限", "违约", "评级下调",
    ]),
    (2.0, 0, "财政与政治", [
        "stimulus", "budget", "fiscal", "tax cut", "election", "nominate",
        "刺激", "财政", "预算", "减税", "选举", "提名",
    ]),
    (2.2, 0, "公司与行业", [
        "earnings beat", "earnings miss", "guidance", "profit warning",
        "layoffs", "acquisition", "merger", "capex",
        "财报", "业绩指引", "盈利预警", "裁员", "收购", "并购", "资本开支",
    ]),
    (1.8, None, "货币政策", [
        "treasury yield", "bond yield", "10-year", "yield curve", "dollar index",
        "美债收益率", "十年期", "收益率曲线", "美元指数",
    ]),
]

COPPER_MAJORS = [
    "codelco", "escondida", "grasberg", "las bambas", "cobre panama", "kamoa",
    "quellaveco", "antofagasta", "freeport", "glencore", "teck", "ivanhoe",
    "antamina", "collahuasi", "oyu tolgoi", "el teniente", "chuquicamata",
    "紫金", "洛阳钼业", "江西铜业", "铜陵有色", "云南铜业", "金川",
]
MACRO_MAJORS = [
    "powell", "trump", "yellen", "bessent", "lagarde", "ueda",
    "鲍威尔", "特朗普", "拉加德", "植田",
]

SOURCE_BONUS = {
    "美联储官网": 1.0,
    "SHMET铜快讯": 0.8,
    "GN-美联储与利率": 0.5,
    "GN-美国经济数据": 0.5,
    "GN-华尔街": 0.5,
    "GN-铜供应中断": 0.5,
    "GN-铜库存政策": 0.4,
    "财联社电报": 0.3,
    "Mining.com": 0.3,
}

_UP = ["rise", "rose", "rises", "increase", "higher", "up ", "climb", "jump",
       "上升", "增加", "上涨", "累库", "回升", "走高"]
_DOWN = ["fall", "fell", "falls", "decline", "lower", "down ", "drop", "slump",
         "下降", "减少", "下滑", "去库", "回落", "走低"]

_NUM = re.compile(
    r"\d[\d,.]*\s*(%|吨|万吨|美元|元/吨|基点|bps?|tonnes?|tons?|kt\b|mt\b|usd|\$)",
    re.IGNORECASE)

SPEC = {
    "铜": (COPPER_SIGNALS, COPPER_MAJORS),
    "宏观": (MACRO_SIGNALS, MACRO_MAJORS),
}


def _direction_from_context(hay, base):
    """base=None 的类别（库存、加工费、收益率）才看涨跌定方向。
    其余方向写死在词表里，判不了就返回 0，不硬猜。"""
    if base is not None:
        return base
    up = sum(1 for w in _UP if w in hay)
    down = sum(1 for w in _DOWN if w in hay)
    if up > down:
        return -1      # 库存累积 / 收益率上行 → 利空
    if down > up:
        return +1
    return 0


def score(item, bucket):
    signals, majors = SPEC[bucket]
    hay = f"{item.title} {item.summary}".lower()

    total, votes, strongest = 0.0, 0.0, 0.0
    cats = {}
    for weight, base_dir, category, words in signals:
        hits = sum(1 for w in words if w in hay)
        if not hits:
            continue
        w = weight * (1 + 0.3 * min(hits - 1, 2))   # 同组命中收益递减，防堆词刷分
        total += w
        strongest = max(strongest, weight)
        cats[category] = cats.get(category, 0) + w
        votes += w * _direction_from_context(hay, base_dir)

    if any(m in hay for m in majors):
        total += 1.0
    if _NUM.search(f"{item.title} {item.summary}"):
        total += 0.8      # 带具体数字的条目信息量更高
    total += SOURCE_BONUS.get(item.source, 0.0)

    if total >= 7.5:
        importance = 5
    elif total >= 5.0:
        importance = 4
    elif total >= 3.2:
        importance = 3
    elif total >= 1.8:
        importance = 2
    else:
        importance = 1

    # 硬事件兜底：罢工/停产/降息/开战这类，哪怕总分不高也至少 4 星，
    # 免得被词频堆出来的高分盖过去
    if strongest >= 2.5:
        importance = max(importance, 4)

    return {
        "relevant": total >= 1.5,
        "importance": importance,
        "category": max(cats, key=cats.get) if cats else "其他",
        "direction": "利多" if votes > 0.5 else "利空" if votes < -0.5 else "中性",
        "title_zh": item.title,          # 规则档不翻译，保留原标题
        "summary_zh": item.summary[:200],
    }


class RuleJudge:
    """接口跟 LLM 后端一致，news.py 直接替换即可。"""
    model = "rules(本地规则，零成本)"

    def judge(self, bucket, items, batch_size=0):
        return [(it, score(it, bucket)) for it in items]

    def cost_usd(self):
        return 0.0
