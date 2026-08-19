#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""隔夜新闻：抓取 → 去重 → 分桶粗筛 → Claude 判定 → 按重要性排序。

分工跟行情模块一样严格：Claude 只做「这条重不重要、说的是什么事」，
一个数字都不许算——涨跌幅、库存、价格全部由 quotes.py 那边的实测值负责。

粗筛用的是本地关键词表，作用只有一个：别让明显无关的条目去花 Claude 的钱。
真正的取舍交给模型。
"""
import os
import sys
import json
import difflib
import datetime as dt
from typing import Literal

import yaml
import anthropic
from pydantic import BaseModel, ConfigDict, ValidationError

from . import sources
from .filters import prefilter

# 从模块自身位置推导项目根，别写死家目录——
# GitHub Actions 的 runner 上没有 ~/Desktop，写死了云端必挂
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG = os.path.join(ROOT, "config.yaml")
CST = dt.timezone(dt.timedelta(hours=8))


def log(msg):
    print(f"[{dt.datetime.now():%H:%M:%S}] {msg}", file=sys.stderr, flush=True)


def load_config():
    with open(CONFIG, encoding="utf-8") as f:
        return yaml.safe_load(f)


def window(cfg, now=None):
    """隔夜窗 = 北京时间前一日 16:00 → 运行时刻。

    用绝对时刻而不是「往回 N 小时」：手动补跑或 Actions 排队延迟时，
    窗口边界不会跟着漂，同一个交易日重跑两次拿到的是同一批新闻。
    """
    now = now or dt.datetime.now(CST)
    start = (now - dt.timedelta(days=1)).replace(
        hour=cfg["news"]["window_start_hour"], minute=0, second=0, microsecond=0)
    return start.astimezone(dt.timezone.utc), now.astimezone(dt.timezone.utc)


def fetch(cfg, start):
    """抓全部源。单源失败不影响整轮——GitHub Actions 的美国 IP 访问国内源尤其容易挂。"""
    items, failures = [], []
    for src in cfg["news"]["sources"]:
        fetcher = sources.FETCHERS.get(src["type"])
        if fetcher is None:
            failures.append(f"{src['name']}: 未知源类型 {src['type']}")
            continue
        try:
            got = fetcher(src)
        except Exception as e:
            failures.append(f"{src['name']}: {type(e).__name__}: {str(e)[:80]}")
            log(f"  源 {src['name']} 挂了：{type(e).__name__}")
            continue
        fresh = [i for i in got if i.published >= start and i.title]
        log(f"  {src['name']:<16} 抓到 {len(got):3d} 条，窗口内 {len(fresh):3d} 条")
        items.extend(fresh)
    items.sort(key=lambda i: i.published, reverse=True)
    return items, failures


def dedup(items, threshold):
    """精确指纹 + 标题模糊比对。同一事件多家转载只留最早的那条。"""
    seen_uid, kept, titles = set(), [], []
    for it in items:
        if it.uid in seen_uid:
            continue
        nt = sources.norm_title(it.title)
        if not nt:
            continue
        if any(difflib.SequenceMatcher(None, nt, old).ratio() >= threshold for old in titles):
            continue
        seen_uid.add(it.uid)
        titles.append(nt)
        kept.append(it)
    return kept


def dedup_judged(rows, threshold):
    """判定之后按中文标题再去一次重。

    第一次去重比的是原始标题，而四家外媒报同一件事的英文措辞各不相同，
    翻成中文才现出原形——实测「30年期美债收益率创19年新高」这一件事
    能占掉宏观栏 8 个位置里的 4 个。同一事件保留星级最高的那条。
    """
    rows = sorted(rows, key=lambda r: -r["importance"])
    kept, titles = [], []
    for r in rows:
        nt = sources.norm_title(r["title"])
        if any(difflib.SequenceMatcher(None, nt, old).ratio() >= threshold for old in titles):
            continue
        titles.append(nt)
        kept.append(r)
    return kept


def cap_per_source(items, cap):
    """每个源最多留 cap 条（按时间新→旧）。

    不加这个限制，某一组 Google News 查询单独就能贡献七十多条，
    把美联储 RSS、财联社这些信噪比更高的源挤出送审名单。
    """
    count, kept = {}, []
    for it in items:
        n = count.get(it.source, 0)
        if n >= cap:
            continue
        count[it.source] = n + 1
        kept.append(it)
    return kept


# ------------------------------------------------------------------ Claude 判定

MACRO_CATS = ("货币政策", "经济数据", "财政与政治", "关税与贸易", "地缘与能源", "公司与行业", "其他")
COPPER_CATS = ("供应", "需求", "库存", "宏观政策", "价格交易", "公司项目", "其他")
DIRECTIONS = ("利多", "利空", "中性")


class MacroJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    relevant: bool
    importance: int
    category: Literal[MACRO_CATS]  # type: ignore[valid-type]
    direction: Literal[DIRECTIONS]  # type: ignore[valid-type]
    title_zh: str
    summary_zh: str


class CopperJudgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int
    relevant: bool
    importance: int
    category: Literal[COPPER_CATS]  # type: ignore[valid-type]
    direction: Literal[DIRECTIONS]  # type: ignore[valid-type]
    title_zh: str
    summary_zh: str


class MacroBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[MacroJudgement]


class CopperBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[CopperJudgement]


COMMON_RULES = """
importance 打分（1-5）：
5 = 隔夜最重要的事，不看会漏掉行情主线
4 = 明确改变预期，值得单独说一句
3 = 值得记一笔的边际变化
2 = 背景信息
1 = 几乎无信息量
不相关的条目 importance 一律给 1。

title_zh：25 字以内中文标题，直接说事，别用「关于……的消息」这种废话。
summary_zh：1-2 句话，必须包含标题里没有的具体信息（数字、时间、主体、幅度）。
            原文只有标题时，就写清楚这条为什么重要，不要复述标题。
            英文源翻译成中文，不要保留英文原句。

不要编造原文里没有的数字。你拿到的只有标题和摘要，没有行情数据——
涨跌幅、价格、库存这些由程序另行计算，你不需要也不允许自己推算。

每一条输入都必须在输出里有对应的 id，不能漏，也不能编造不存在的 id。
"""

MACRO_SYSTEM = """你是一名宏观研究员，服务于一位做大宗商品为主的研究员。他每天早上 9 点
看一份隔夜早报，你的任务是从一批原始新闻里挑出**昨夜真正发生的宏观大事**，并做中文速读。

判断「相关」（relevant=true）：
- 货币政策：美联储与其他主要央行的决议、纪要、官员讲话、利率预期变化
- 经济数据：CPI/PPI/PCE、非农与就业、GDP、零售、PMI/ISM、房地产数据的实际公布值
- 财政与政治：预算与债务上限、政府停摆、选举与人事、监管重大变化
- 关税与贸易：关税落地或威胁、出口管制、制裁、贸易协定
- 地缘与能源：冲突、OPEC 决策、供应中断，尤其是会传导到油价和金价的
- 公司与行业：会牵动整个板块的大型公司财报、指引、并购（单纯个股点评不算）

判断「不相关」（relevant=false）：
- 券商观点、荐股、目标价、技术面喊单
- 昨夜之前的旧闻重发、纯预告性的「本周关注」
- 没有新信息的行情复盘（「美股收涨」这类，行情数据我们自己有）
- 与金融市场无关的社会新闻

direction 是对**风险资产（美股）**的方向影响：利多 / 利空 / 中性。
""" + COMMON_RULES

COPPER_SYSTEM = """你是一名铜产业链基本面研究员，服务于一位做铜为主的大宗商品研究员。
你的任务是从一批原始新闻里挑出**真正影响铜基本面或铜价的信息**，并做中文速读。

判断「相关」（relevant=true）：
- 铜矿供应：罢工、事故、停产、检修、品位下滑、投产爬产、许可与社区冲突、指引调整
- 冶炼与加工费：TC/RC 变化、冶炼厂检修减产、粗炼精炼产能变动
- 库存与贸易流：LME/COMEX/SHFE/保税区库存异动、升贴水、跨市套利、进出口与关税配额
- 需求：电网投资、新能源车/光伏/家电排产、地产竣工、再生铜供应
- 宏观政策：与铜直接相关的关税、出口禁令、制裁、资源国税收与国有化
- 权威平衡表与预测：ICSG、Cochilco、主要投行的供需平衡与价格预测调整

判断「不相关」（relevant=false）：
- 纯股价涨跌点评、券商荐股、技术面喊单
- 只是顺带提到铜的泛金属/泛市场综述
- 与铜基本面无关的公司公告（高管变动、ESG 报告、赞助）
- 含「铜」字但无关的内容（铜像、铜牌、铜锣湾等）

direction 是对**铜价**的方向影响：利多 / 利空 / 中性。
""" + COMMON_RULES

ALU_SYSTEM = """你是一名铝产业链基本面研究员，服务于一位做有色为主的大宗商品研究员。
你的任务是从一批原始新闻里挑出**真正影响铝基本面或铝价的信息**，并做中文速读。

判断「相关」（relevant=true）：
- 电解铝供应：投复产、减产、限电、槽数变动、运行产能变化、新建产能投放
- 上游：氧化铝产能与开工、铝土矿供应（几内亚、澳洲、印尼的出口政策与船期）、动力煤与电价
- 库存与贸易流：LME/SHFE/社会库存异动、保税区、现货升贴水、出口退税与关税
- 需求：地产竣工、汽车轻量化、光伏边框、特高压、包装与家电排产、型材开工
- 政策：产能天花板、能耗双控、碳交易、俄铝制裁与相关贸易限制
- 权威平衡表与预测：IAI、安泰科、主要投行的供需平衡与价格预测调整

判断「不相关」（relevant=false）：
- 纯股价涨跌点评、券商荐股、技术面喊单
- 铝制消费品零售（门窗、餐盒、箱包）
- 与铝基本面无关的公司公告（高管变动、ESG 报告、赞助）

direction 是对**铝价**的方向影响：利多 / 利空 / 中性。
""" + COMMON_RULES

BUCKET_SPEC = {
    "宏观": (MACRO_SYSTEM, MacroBatch),
    "铜": (COPPER_SYSTEM, CopperBatch),
    "铝": (ALU_SYSTEM, CopperBatch),   # 分类集跟铜一样，复用同一个 schema
}


def _render(items):
    return "\n\n".join(
        f"[{i}] 来源: {it.source} | 时间: {it.published.astimezone(CST):%m-%d %H:%M}\n"
        f"标题: {it.title}\n"
        f"摘要: {it.summary[:300] or '(无)'}"
        for i, it in enumerate(items)
    )


class ClaudeJudge:
    def __init__(self, cfg):
        self.client = anthropic.Anthropic()
        self.model = os.environ.get("BRIEF_MODEL") or cfg["judge"]["claude"]["id"]
        self.effort = cfg["judge"]["claude"]["effort"]
        self.usage = {"input": 0, "output": 0}

    def _call(self, bucket, items):
        system, schema_model = BUCKET_SPEC[bucket]
        try:
            resp = self.client.messages.create(
                model=self.model,
                max_tokens=16000,
                system=system,
                output_config={
                    "effort": self.effort,
                    "format": {"type": "json_schema", "schema": schema_model.model_json_schema()},
                },
                messages=[{
                    "role": "user",
                    "content": f"以下是 {len(items)} 条待判断的隔夜新闻，请逐条给出结论：\n\n" + _render(items),
                }],
            )
        except anthropic.APIStatusError as e:
            log(f"  Claude 调用失败 HTTP {e.status_code}: {str(e)[:150]}")
            return []
        except anthropic.APIConnectionError as e:
            log(f"  连不上 Claude API: {e}")
            return []

        self.usage["input"] += resp.usage.input_tokens
        self.usage["output"] += resp.usage.output_tokens

        if resp.stop_reason == "refusal":
            log("  模型拒答了这一批，跳过")
            return []
        if resp.stop_reason == "max_tokens":
            log("  这一批被 max_tokens 截断，结果可能不完整")

        text = next((b.text for b in resp.content if b.type == "text"), "")
        try:
            return schema_model.model_validate_json(text).items
        except (ValidationError, json.JSONDecodeError) as e:
            log(f"  解析模型输出失败: {str(e)[:120]}")
            return []

    def judge(self, bucket, items, batch_size):
        """返回 (原条目, 判断) 配对。某一批解析失败就整批丢掉，不阻塞流程。"""
        paired = []
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            log(f"  判定{bucket} 第 {start + 1}-{start + len(chunk)} 条")
            for j in self._call(bucket, chunk):
                if 0 <= j.id < len(chunk):
                    paired.append((chunk[j.id], j.model_dump(exclude={"id"})))
                else:
                    log(f"  模型返回越界 id={j.id}，忽略")
        return paired

    def cost_usd(self):
        price = {
            "claude-sonnet-5": (3.0, 15.0),
            "claude-opus-5": (5.0, 25.0),
            "claude-haiku-4-5": (1.0, 5.0),
        }.get(self.model, (3.0, 15.0))
        return self.usage["input"] / 1e6 * price[0] + self.usage["output"] / 1e6 * price[1]


def make_judge(cfg):
    """三档后端，改 config.yaml 的 judge.backend 一行切换。

    rules          零 key 零成本，但英文标题不翻译，也没有串讲
    openai_compat  接各家免费额度（Gemini / Groq / 智谱），有中文摘要
    claude         质量最好，要付费
    """
    backend = cfg["judge"]["backend"]
    if backend == "rules":
        from .rules import RuleJudge
        return RuleJudge()
    if backend == "openai_compat":
        from .compat import CompatJudge
        return CompatJudge(cfg["judge"]["openai_compat"])
    if backend == "claude":
        return ClaudeJudge(cfg)
    raise ValueError(f"未知的 judge.backend: {backend}")


def collect(cfg=None):
    cfg = cfg or load_config()
    nc = cfg["news"]
    start, end = window(cfg)
    log(f"隔夜窗 {start.astimezone(CST):%m-%d %H:%M} → {end.astimezone(CST):%m-%d %H:%M}（北京时间）")

    raw, failures = fetch(cfg, start)
    items = dedup(raw, nc["fuzzy_dedup_threshold"])
    log(f"窗口内 {len(raw)} 条，去重后 {len(items)} 条")

    judge = make_judge(cfg)
    log(f"判定后端：{judge.model if hasattr(judge, 'model') else cfg['judge']['backend']}")
    out, claimed = {}, set()
    # 按 precedence 从小到大处理：每条只进一个板块，铜的消息不会同时占满宏观栏
    for bucket, bc in sorted(nc["buckets"].items(), key=lambda kv: kv[1]["precedence"]):
        cand = cap_per_source(
            [i for i in prefilter(items, bc["keywords"]) if i.uid not in claimed],
            nc["max_per_source"],
        )[: nc["max_llm_items"]]
        claimed.update(i.uid for i in cand)
        log(f"{bucket}：粗筛出 {len(cand)} 条送判定")
        rows = []
        for it, j in judge.judge(bucket, cand, nc["llm_batch_size"]):
            if not j["relevant"] or j["importance"] < bc["min_importance"]:
                continue
            rows.append({
                "title": j["title_zh"],
                "summary": j["summary_zh"],
                "category": j["category"],
                "direction": j["direction"],
                "importance": j["importance"],
                "source": it.source,
                "url": it.url,
                "published": it.published.astimezone(CST).isoformat(timespec="minutes"),
            })
        merged = dedup_judged(rows, nc["fuzzy_dedup_threshold"])
        if len(merged) < len(rows):
            log(f"{bucket}：判定后合并同题 {len(rows) - len(merged)} 条")
        merged.sort(key=lambda r: (-r["importance"], r["published"]))
        # 同一件事换四种措辞，字面相似度堵不住（实测美加关税一条新闻占了 8 个位置里的 4 个），
        # 按分类限流才能保证版面上是四五件不同的事，而不是一件事的四种说法
        per_cat, picked = {}, []
        for r in merged:
            c = r["category"]
            if per_cat.get(c, 0) >= nc["max_per_category"]:
                continue
            per_cat[c] = per_cat.get(c, 0) + 1
            picked.append(r)
            if len(picked) >= bc["top_n"]:
                break
        out[bucket] = picked
        log(f"{bucket}：留下 {len(out[bucket])} 条")

    out["meta"] = {
        "window_start": start.astimezone(CST).isoformat(timespec="minutes"),
        "window_end": end.astimezone(CST).isoformat(timespec="minutes"),
        "raw_count": len(raw),
        "dedup_count": len(items),
        "failures": failures,
        "llm_cost_usd": round(judge.cost_usd(), 4),
        "llm_model": judge.model,
    }
    log(f"本轮 Claude 花费约 ${judge.cost_usd():.4f}")
    return out


if __name__ == "__main__":
    print(json.dumps(collect(), ensure_ascii=False, indent=2))
