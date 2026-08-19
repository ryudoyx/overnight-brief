#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""事实包 -> 单文件 HTML。

零外部依赖：图全是手写内联 SVG，页面可以直接双击打开，也可以扔上 GitHub Pages。
配色沿用 ~/futures_trend 那套（红涨绿跌 + 同一组 CSS 变量），两个看板看着像一家的。
"""
import os
import json
import html
import datetime as dt

# 从模块自身位置推导项目根，别写死家目录——
# GitHub Actions 的 runner 上没有 ~/Desktop，写死了云端必挂
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE = os.path.join(ROOT, "archive")

CSS = """
:root{--bg:#fafafa;--fg:#1a1a1a;--dim:#6b7280;--line:#e5e7eb;--card:#fff;
--up:#c0392b;--dn:#1e8449;--upb:#fdecea;--dnb:#e8f5ee;--flat:#9ca3af;--warn:#b45309}
@media (prefers-color-scheme:dark){:root{--bg:#16181c;--fg:#e8eaed;--dim:#9aa0a6;
--line:#2a2d33;--card:#1e2126;--up:#e8695a;--dn:#4fb37c;--upb:#3a1f1c;--dnb:#16301f;--warn:#fbbf24}}
*{box-sizing:border-box}
body{margin:0;padding:22px 18px 60px;background:var(--bg);color:var(--fg);
font:14px/1.6 -apple-system,BlinkMacSystemFont,"PingFang SC","Helvetica Neue",sans-serif;
max-width:1080px;margin-inline:auto}
h1{font-size:20px;margin:0 0 2px}
h2{font-size:14px;margin:26px 0 10px;padding-bottom:5px;border-bottom:1px solid var(--line)}
h2 .n{color:var(--dim);font-weight:400;font-size:12px;margin-left:6px}
.sub{color:var(--dim);font-size:12.5px}
.top{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap}
select{background:var(--card);color:var(--fg);border:1px solid var(--line);
border-radius:5px;padding:3px 6px;font-size:12.5px}
.tone{font-size:17px;font-weight:650;margin:14px 0 6px}
.narr{background:var(--card);border:1px solid var(--line);border-radius:7px;
padding:13px 15px;line-height:1.85}
.up{color:var(--up)}.dn{color:var(--dn)}.flat{color:var(--flat)}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(118px,1fr));gap:8px}
.kpi{background:var(--card);border:1px solid var(--line);border-radius:7px;padding:9px 11px}
.kpi .k{font-size:11.5px;color:var(--dim)}
.kpi .v{font-size:17px;font-weight:650;margin-top:1px;font-variant-numeric:tabular-nums}
.kpi .c{font-size:12px;font-variant-numeric:tabular-nums}
table{width:100%;border-collapse:collapse;font-variant-numeric:tabular-nums}
th{text-align:right;padding:5px 7px;color:var(--dim);font-weight:500;font-size:11px;
border-bottom:1px solid var(--line)}
td{text-align:right;padding:6px 7px;border-bottom:1px solid var(--line);font-size:13px}
th:first-child,td:first-child{text-align:left}
tr:last-child td{border-bottom:0}
.news{list-style:none;padding:0;margin:0}
.news li{background:var(--card);border:1px solid var(--line);border-radius:7px;
padding:10px 13px;margin-bottom:7px}
.news .t{font-weight:600}
.news .s{color:var(--dim);font-size:12.5px;margin-top:3px}
.news .m{color:var(--dim);font-size:11.5px;margin-top:5px}
.news a{color:inherit}
.tag{display:inline-block;padding:0 5px;border-radius:3px;font-size:11px;
font-weight:650;margin-right:5px}
.tag.b{background:var(--upb);color:var(--up)}
.tag.s{background:var(--dnb);color:var(--dn)}
.tag.n{background:var(--line);color:var(--dim)}
.cols{display:grid;grid-template-columns:1fr 1fr;gap:18px}
@media (max-width:680px){.cols{grid-template-columns:1fr}}
.note{color:var(--dim);font-size:11.5px;margin-top:26px;line-height:1.8}
.warn{color:var(--warn)}
.wrap{overflow-x:auto}
"""


def esc(s):
    return html.escape(str(s if s is not None else ""))


def cls(v):
    return "up" if (v or 0) > 0 else ("dn" if (v or 0) < 0 else "flat")


def pct(v, nd=2):
    return "—" if v is None else f"{v:+.{nd}f}%"


def num(v):
    if v is None:
        return "—"
    return f"{v:,.4f}".rstrip("0").rstrip(".") if abs(v) < 10 else f"{v:,.2f}"


# ---------------------------------------------------------------- SVG 构件

def sector_bars(rows, w=560, rh=22):
    """板块涨跌横向条形图，零点居中。"""
    rows = [r for r in rows if r.get("chg_pct") is not None]
    if not rows:
        return ""
    span = max(abs(r["chg_pct"]) for r in rows) or 1
    mid, half = w * 0.42, w * 0.42 - 8
    h = len(rows) * rh + 6
    out = [f'<svg viewBox="0 0 {w} {h}" width="100%" height="{h}" role="img" '
           f'aria-label="板块涨跌幅">']
    out.append(f'<line x1="{mid}" y1="0" x2="{mid}" y2="{h - 6}" stroke="var(--line)"/>')
    for i, r in enumerate(rows):
        y, v = i * rh + 3, r["chg_pct"]
        bw = abs(v) / span * half
        x = mid if v >= 0 else mid - bw
        color = "var(--up)" if v > 0 else ("var(--dn)" if v < 0 else "var(--flat)")
        name = esc(r["name"]) + ("·" if r.get("extra") else "")
        out.append(
            f'<rect x="{x:.1f}" y="{y}" width="{bw:.1f}" height="{rh - 8}" rx="2" fill="{color}"/>'
            f'<text x="{mid - half - 6:.1f}" y="{y + rh - 11}" text-anchor="end" '
            f'font-size="12" fill="var(--fg)">{name}</text>'
            f'<text x="{w - 4}" y="{y + rh - 11}" text-anchor="end" font-size="12" '
            f'fill="{color}" font-weight="600">{v:+.2f}%</text>')
    out.append("</svg>")
    return "".join(out)


def spark(vals, w=104, h=26):
    """迷你走势线。收在最后一点，颜色跟首尾方向一致。"""
    vals = [v for v in (vals or []) if v is not None]
    if len(vals) < 2:
        return ""
    lo, hi = min(vals), max(vals)
    rng = (hi - lo) or 1
    step = (w - 4) / (len(vals) - 1)
    pts = " ".join(f"{2 + i * step:.1f},{h - 3 - (v - lo) / rng * (h - 6):.1f}"
                   for i, v in enumerate(vals))
    color = "var(--up)" if vals[-1] >= vals[0] else "var(--dn)"
    lx, ly = pts.split()[-1].split(",")
    return (f'<svg viewBox="0 0 {w} {h}" width="{w}" height="{h}" aria-hidden="true">'
            f'<polyline points="{pts}" fill="none" stroke="{color}" stroke-width="1.5" '
            f'stroke-linejoin="round"/><circle cx="{lx}" cy="{ly}" r="2" fill="{color}"/></svg>')


# ---------------------------------------------------------------- 版块

def kpi_block(rows, unit_suffix=""):
    out = ['<div class="kpis">']
    for r in rows:
        out.append(
            f'<div class="kpi"><div class="k">{esc(r["name"])}</div>'
            f'<div class="v">{num(r["close"])}{unit_suffix}</div>'
            f'<div class="c {cls(r["chg_pct"])}">{pct(r["chg_pct"])}</div></div>')
    out.append("</div>")
    return "".join(out)


def news_block(rows, empty="隔夜窗内没有达到阈值的消息。"):
    if not rows:
        return f'<div class="sub">{empty}</div>'
    out = ['<ul class="news">']
    for r in rows:
        d = r["direction"]
        tag = "b" if d == "利多" else ("s" if d == "利空" else "n")
        stars = "★" * r["importance"] + "☆" * (5 - r["importance"])
        url = esc(r.get("url") or "")
        title = esc(r["title"])
        link = f'<a href="{url}" target="_blank" rel="noopener">{title}</a>' if url else title
        out.append(
            f'<li><div class="t"><span class="tag {tag}">{esc(d)}</span>{link}</div>'
            f'<div class="s">{esc(r["summary"])}</div>'
            f'<div class="m">{esc(r["category"])} · {stars} · {esc(r["source"])} · '
            f'{esc(r["published"][5:16].replace("T", " "))}</div></li>')
    out.append("</ul>")
    return "".join(out)


def movers_table(rows, head):
    if not rows:
        return f"<h3 class='sub'>{head}</h3><div class='sub'>无</div>"
    body = "".join(
        f'<tr><td>{esc(r["sym"])} <span class="sub">{esc(str(r["name"])[:16])}</span></td>'
        f'<td>{num(r["close"])}</td>'
        f'<td class="{cls(r["chg_pct"])}">{pct(r["chg_pct"])}</td>'
        f'<td class="sub">{r["market_cap"] / 1e9:,.0f}B</td></tr>'
        for r in rows)
    return (f'<div class="wrap"><table><thead><tr><th>{esc(head)}</th><th>收盘</th>'
            f'<th>涨跌</th><th>市值</th></tr></thead><tbody>{body}</tbody></table></div>')


def watch_table(rows, group):
    sel = [r for r in rows if r["group"] == group]
    if not sel:
        return ""
    sel.sort(key=lambda r: -(r["chg_pct"] or 0))
    def vol(r):
        v = r.get("vol_ratio")
        return "—" if v is None else f"{v:.2f}x"

    body = "".join(
        f'<tr><td>{esc(r["name"])} <span class="sub">{esc(r["sym"])}</span></td>'
        f'<td>{num(r["close"])}</td>'
        f'<td class="{cls(r["chg_pct"])}">{pct(r["chg_pct"])}</td>'
        f'<td class="sub">{vol(r)}</td></tr>'
        for r in sel)
    return (f'<div class="wrap"><table><thead><tr><th>{esc(group)}</th><th>收盘</th>'
            f'<th>涨跌</th><th>量比</th></tr></thead><tbody>{body}</tbody></table></div>')


def commodity_table(rows):
    body = "".join(
        f'<tr><td>{esc(r["name"])}</td><td>{num(r["close"])}</td>'
        f'<td class="sub">{esc(r["unit"])}</td>'
        f'<td class="{cls(r["chg_pct"])}">{pct(r["chg_pct"])}</td>'
        f'<td style="width:110px">{spark(r["spark"])}</td></tr>'
        for r in rows)
    return (f'<div class="wrap"><table><thead><tr><th>品种</th><th>最新</th><th>单位</th>'
            f'<th>隔夜</th><th style="text-align:right">近 10 日</th></tr></thead>'
            f'<tbody>{body}</tbody></table></div>')


def date_picker(current):
    days = sorted((f[:-5] for f in os.listdir(ARCHIVE) if f.endswith(".json")), reverse=True)
    if current not in days:
        days.insert(0, current)
    opts = "".join(
        f'<option value="{d}.html"{" selected" if d == current else ""}>{d}</option>'
        for d in days[:120])
    return (f'<select onchange="location.href=this.value">{opts}</select>')


def comment_block(pack, variety):
    c = (pack.get("commentary") or {}).get(variety)
    if not c:
        return ""
    return (f'<div class="tone">{esc(c.get("tone", ""))}</div>'
            f'<div class="narr">{esc(c.get("text", ""))}</div>')


def metal_kpis(pack, names):
    """交易所库存 + 沪盘，拼成 KPI 卡。"""
    m = (pack.get("metals") or {}).get("varieties", {})
    rows = []
    for cn in names:
        v = m.get(cn) or {}
        qt, inv = v.get("quote"), v.get("inventory")
        if qt:
            rows.append({"name": f"沪{cn}" if cn != "氧化铝" else "氧化铝",
                         "close": qt["close"], "chg_pct": qt["chg_pct"]})
        if inv:
            rows.append({"name": f"{cn}交易所库存", "close": inv["stock"],
                         "chg_pct": None, "sub": inv["change"]})
    if not rows:
        return ""
    out = ['<div class="kpis">']
    for r in rows:
        extra = (f'<div class="c {cls(r["sub"])}">{r["sub"]:+,.0f} 吨</div>'
                 if r.get("sub") is not None else
                 f'<div class="c {cls(r["chg_pct"])}">{pct(r["chg_pct"])}</div>')
        out.append(f'<div class="kpi"><div class="k">{esc(r["name"])}</div>'
                   f'<div class="v">{num(r["close"])}</div>{extra}</div>')
    out.append("</div>")
    return "".join(out)


def build(pack):
    q, n = pack["quotes"], pack["news"]
    narr = pack.get("narration") or {}
    day = q["meta"]["session_date"]
    gen = pack["meta"]["generated_at"][:16].replace("T", " ")

    idx = q["indices"]
    sectors_main = [r for r in q["sectors"] if not r.get("extra")]
    sectors_all = q["sectors"]

    parts = [
        f'<div class="top"><div><h1>隔夜早报 · {esc(day)}</h1>'
        f'<div class="sub">美股 {esc(day)} 收盘 · 生成于 {esc(gen)}</div></div>'
        f'{date_picker(day)}</div>',
    ]
    if narr.get("tone"):
        parts.append(f'<div class="tone">{esc(narr["tone"])}</div>')
    if narr.get("text"):
        parts.append(f'<div class="narr">{esc(narr["text"])}</div>')

    parts.append(f'<h2>宏观大事<span class="n">{len(n.get("宏观", []))} 条</span></h2>')
    parts.append(news_block(n.get("宏观", [])))

    parts.append("<h2>美股</h2>")
    parts.append(kpi_block(idx))
    parts.append('<h2>板块<span class="n">SPDR 11 大板块，带 · 的为参考指标</span></h2>')
    parts.append(sector_bars(sectors_all))
    parts.append('<h2>个股异动<span class="n">市值 200 亿美元以上</span></h2>')
    parts.append('<div class="cols">'
                 + movers_table(q["movers"]["up"], "领涨")
                 + movers_table(q["movers"]["down"], "领跌") + "</div>")
    parts.append("<h2>自选池</h2>")
    parts.append('<div class="cols">' + watch_table(q["watchlist"], "矿业")
                 + watch_table(q["watchlist"], "中概") + "</div>")

    parts.append("<h2>利率与汇率</h2>")
    parts.append(kpi_block(q["macro_markets"]))

    parts.append("<h2>商品</h2>")
    parts.append(commodity_table(q["commodities"]))

    c = q["copper"]
    parts.append("<h2>铜</h2>")
    parts.append(comment_block(pack, "铜"))
    parts.append(metal_kpis(pack, ["铜"]))
    cop = []
    if c.get("comex"):
        r = c["comex"]
        cop.append({"name": "COMEX 铜", "close": r["close"], "chg_pct": r["chg_pct"]})
    if c.get("shfe_night"):
        s = c["shfe_night"]
        cop.append({"name": "沪铜夜盘", "close": s["close"], "chg_pct": None})
    if cop:
        parts.append(kpi_block(cop))
    if c.get("miners"):
        parts.append('<div class="cols">' + watch_table(c["miners"], "矿业") + "</div>")
    parts.append(news_block(n.get("铜", []), "隔夜窗内没有达到阈值的铜消息。"))

    parts.append("<h2>铝</h2>")
    parts.append(comment_block(pack, "铝"))
    parts.append(metal_kpis(pack, ["铝", "氧化铝"]))
    parts.append(news_block(n.get("铝", []), "隔夜窗内没有达到阈值的铝消息。"))

    nm = n.get("meta", {})
    fails = nm.get("failures") or []
    note = [f'数据窗口 {esc(nm.get("window_start", ""))} → {esc(nm.get("window_end", ""))}（北京时间）。',
            f'原始 {nm.get("raw_count", 0)} 条，去重后 {nm.get("dedup_count", 0)} 条。',
            f'行情来自 Yahoo Finance，铜价单位已统一为 USD/t；沪铜夜盘来自新浪。']
    if pack["meta"].get("cost_usd"):
        note.append(f'本次 LLM 花费约 ${pack["meta"]["cost_usd"]:.4f}（{esc(nm.get("llm_model", ""))}）。')
    gaps = (pack.get("metals") or {}).get("gaps") or []
    if gaps:
        note.append("品种评论未覆盖（免费源拿不到，刻意不写而非遗漏）：" + esc("；".join(gaps)))
    if fails:
        note.append('<span class="warn">失败源：' + esc("；".join(fails)[:300]) + "</span>")
    parts.append('<div class="note">' + "<br>".join(note) + "</div>")

    return (f'<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>隔夜早报 {esc(day)}</title><style>{CSS}</style></head>'
            f'<body>{"".join(parts)}</body></html>')


if __name__ == "__main__":
    import sys
    src = sys.argv[1]
    pack = json.load(open(src, encoding="utf-8"))
    out = os.path.splitext(src)[0] + ".html"
    open(out, "w", encoding="utf-8").write(build(pack))
    print(out)
