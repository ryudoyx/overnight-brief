"""抓取各类新闻源，统一成 Item。搬自 copper-watch，逻辑未改。


每个 fetcher 都自己吞异常：任何一个源挂了（被墙、改版、限流）都不应该
让整轮跑失败——GitHub Actions 的美国 IP 访问国内源尤其容易出这种事。
"""

from __future__ import annotations

import hashlib
import html
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import feedparser
import requests

log = logging.getLogger(__name__)

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
TIMEOUT = 25


@dataclass
class Item:
    title: str
    summary: str
    url: str
    source: str
    published: datetime  # 一律 UTC aware
    lang: str

    @property
    def uid(self) -> str:
        """精确去重指纹：标题 + 链接。"""
        key = f"{norm_title(self.title)}|{canonical_url(self.url)}"
        return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------- 工具

_TRACKING = re.compile(r"[?&](utm_[^=]+|from|spm|ref|src)=[^&]*")


def canonical_url(url: str) -> str:
    url = _TRACKING.sub("", url or "")
    return url.rstrip("?&/").lower()


def norm_title(title: str) -> str:
    """去掉 Google News 的 " - 媒体名" 后缀、标点和空白，便于比对。"""
    t = html.unescape(title or "")
    t = re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", t)  # 尾部媒体署名
    t = re.sub(r"[\s\W_]+", "", t, flags=re.UNICODE)
    return t.lower()


def clean_title(title: str) -> str:
    return re.sub(r"\s+[-–—]\s+[^-–—]{2,40}$", "", html.unescape(title or "")).strip()


def strip_html(text: str, limit: int = 400) -> str:
    text = re.sub(r"<[^>]+>", " ", html.unescape(text or ""))
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _utc(ts) -> datetime:
    if ts is None:
        return datetime.now(timezone.utc)
    return datetime.fromtimestamp(time.mktime(ts), tz=timezone.utc)


# ------------------------------------------------------------------ fetchers


def fetch_rss(src: dict) -> list[Item]:
    r = requests.get(src["url"], headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    feed = feedparser.parse(r.content)
    out = []
    for e in feed.entries:
        out.append(
            Item(
                title=clean_title(e.get("title", "")),
                summary=strip_html(e.get("summary", "")),
                url=e.get("link", ""),
                source=src["name"],
                published=_utc(e.get("published_parsed") or e.get("updated_parsed")),
                lang=src.get("lang", "en"),
            )
        )
    return out


def fetch_gnews(src: dict) -> list[Item]:
    tail = (
        "hl=zh-CN&gl=CN&ceid=CN:zh-Hans"
        if src.get("lang") == "zh"
        else "hl=en-US&gl=US&ceid=US:en"
    )
    url = (
        "https://news.google.com/rss/search"
        f"?q={urllib.parse.quote(src['query'])}&{tail}"
    )
    return fetch_rss({**src, "url": url})


def fetch_shmet(src: dict) -> list[Item]:
    """上海金属网快讯。channel='铜' 拿的是纯铜频道，信噪比很高。"""
    tags = {
        "全部": None, "要闻": "0", "财经": "999", "铜": "1002", "铝": "1003",
        "铅": "1005", "锌": "1004", "镍": "1006", "锡": "1007",
    }
    channel = src.get("channel", "铜")
    tag = tags.get(channel)
    payload = (
        {"currentPage": 1, "pageSize": 100}
        if tag is None
        else {"currentPage": 1, "pageSize": 100, "content": "", "flashTag": tag}
    )
    r = requests.post(
        "https://www.shmet.com/api/rest/news/queryNewsflashList",
        json=payload,
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = (r.json().get("data") or {}).get("dataList") or []

    out = []
    for row in rows:
        # contentText 是纯文本版；content 带 HTML，只在前者缺失时兜底
        raw = row.get("contentText") or row.get("content") or ""
        content = strip_html(str(raw), limit=2000)
        # SHMET 正文形如 "【标题】SHMET08月06日讯，正文..."
        m = re.match(r"\s*【(.+?)】(.*)", content, flags=re.S)
        title, body = (m.group(1), m.group(2)) if m else (content[:60], content)
        try:
            pub = datetime.fromtimestamp(int(row["pushTime"]) / 1000, tz=timezone.utc)
        except (KeyError, TypeError, ValueError):
            pub = datetime.now(timezone.utc)
        out.append(
            Item(
                title=title.strip(),
                summary=strip_html(body),
                url="https://www.shmet.com/newsFlash/newsFlash.html",
                source=f"{src['name']}",
                published=pub,
                lang="zh",
            )
        )
    return out


def fetch_cls(src: dict) -> list[Item]:
    """财联社电报。sign = md5(sha1(urlencode(params)))。"""
    params = {
        "app": "CailianpressWeb",
        "category": "",
        "last_time": int(time.time()),
        "os": "web",
        "refresh_type": "1",
        "rn": "50",
        "sv": "8.4.6",
    }
    qs = urllib.parse.urlencode(params)
    params["sign"] = hashlib.md5(
        hashlib.sha1(qs.encode()).hexdigest().encode()
    ).hexdigest()

    r = requests.get(
        "https://www.cls.cn/v1/roll/get_roll_list",
        params=params,
        headers={"User-Agent": UA},
        timeout=TIMEOUT,
    )
    r.raise_for_status()
    rows = (r.json().get("data") or {}).get("roll_data") or []

    out = []
    for row in rows:
        title = (row.get("title") or "").strip()
        content = (row.get("content") or "").strip()
        if not title:
            title = content[:60]
        out.append(
            Item(
                title=title,
                summary=strip_html(content),
                url=row.get("shareurl") or "https://www.cls.cn/telegraph",
                source=src["name"],
                published=datetime.fromtimestamp(
                    int(row.get("ctime") or time.time()), tz=timezone.utc
                ),
                lang="zh",
            )
        )
    return out



def fetch_wscn(src: dict) -> list[Item]:
    """华尔街见闻**资讯流**（文章），不是快讯流。

    快讯流（/content/lives）实测 100 条基本是 A 股盘中异动，跟财联社电报生态位
    重合且信噪比更差；资讯流是编辑筛过的文章，窗内 27 条里 8 条宏观相关，
    而且常带机构观点（野村、Jeff Currie 这类），正好配合「观点要点名」的出处规则。
    """
    r = requests.get(
        "https://api-one.wallstcn.com/apiv1/content/information-flow",
        params={"channel": src.get("channel", "global-channel"),
                "accept": "article", "limit": src.get("limit", 40)},
        headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    items = ((r.json().get("data") or {}).get("items")) or []

    out = []
    for it in items:
        res = it.get("resource") or {}
        title = (res.get("title") or "").strip()
        ts = res.get("display_time") or it.get("display_time")
        if not title or not ts:
            continue
        out.append(
            Item(
                title=title,
                summary=strip_html(res.get("content_short") or res.get("summary") or ""),
                url=res.get("uri") or "https://wallstreetcn.com/",
                source=src["name"],
                published=datetime.fromtimestamp(int(ts), tz=timezone.utc),
                lang="zh",
            )
        )
    return out



def fetch_jin10(src: dict) -> list[Item]:
    """金十期货头条。

    鉴权靠两个自定义头（从站点 JS 包里挖出来的），不带就一律 502：
        x-app-id: KxBcVoDHStE6CUkQ    x-version: 1.3.0
    这两个值是硬编码在前端里的公开常量，不是私人凭据；哪天前端换了值，
    这个源会整体 502，fetch_all 会捕获并跳过，不影响出报。

    只有标题、没有正文——detail 接口全部 502，content 字段是个内部 ID 不是文本。
    所以这个源的条目在判定时天然弱于有长摘要的源，主要当作「期货圈今天在看什么」
    的信号，偶尔能捞到独有消息。
    """
    r = requests.get(
        "https://futures-report-api.jin10.com/api/headline",
        headers={"User-Agent": UA, "Referer": "https://qihuo.jin10.com/",
                 "Origin": "https://qihuo.jin10.com",
                 "x-app-id": "KxBcVoDHStE6CUkQ", "x-version": "1.3.0"},
        timeout=TIMEOUT)
    r.raise_for_status()
    rows = ((r.json().get("data") or {}).get("list")) or []

    out = []
    for row in rows:
        title = (row.get("title") or "").strip()
        if not title:
            continue
        try:
            # updated_at 是北京时间的朴素字符串，转成 UTC 才能跟时间窗比
            pub = datetime.strptime(row["updated_at"], "%Y-%m-%d %H:%M:%S").replace(
                tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        except (KeyError, TypeError, ValueError):
            pub = datetime.now(timezone.utc)
        out.append(
            Item(title=title, summary="", url="https://qihuo.jin10.com/",
                 source=src["name"], published=pub, lang="zh")
        )
    return out



_TG_MSG = re.compile(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', re.S)
_TG_TIME = re.compile(r'<time[^>]+datetime="([^"]+)"')


def fetch_telegram(src: dict) -> list[Item]:
    """Telegram 公开频道，走 t.me/s/<频道> 的网页预览。

    不需要 bot token、不需要登录——公开频道的网页预览就是完整的最近消息列表，
    带 ISO 时间戳，而且有正文不只是标题。

    一次只能拿到最近 20 条（网页预览的分页大小）。WalterBloomberg 这类频道
    20 条大约覆盖 20 小时，够我们的隔夜窗；如果哪天某个频道刷屏，早段可能取不全，
    这是这个方案的固有上限。

    注意 t.me/s/ 对私有频道、封停账号、已废弃频道都返回 200 但零消息——
    实测彭博官方频道停更 5 个月、mining_com 停在 2023 年，加源前务必看最新时间戳。
    """
    r = requests.get(f"https://t.me/s/{src['channel']}",
                     headers={"User-Agent": UA}, timeout=TIMEOUT)
    r.raise_for_status()
    msgs = _TG_MSG.findall(r.text)
    times = _TG_TIME.findall(r.text)

    out = []
    for raw, ts in zip(msgs, times):
        # 必须先按 <br> 切段再清洗：strip_html 会把换行压成空格，
        # 先清洗就再也找不到标题和正文的分界了
        segs = [strip_html(x) for x in re.split(r"<br\s*/?>", raw)]
        # 末尾那行 (@频道名) 是转载署名，不是内容
        # 清洗后署名会变成「( @WalterBloomberg )」——括号内带空格，正则要放宽
        segs = [x for x in segs if x and not re.fullmatch(r"[(（]?\s*@[\w]+\s*[)）]?", x)]
        if not segs:
            continue
        title = segs[0][:120]
        body = " ".join(segs[1:])
        try:
            pub = datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue
        out.append(
            Item(title=title, summary=body.strip()[:400],
                 url=f"https://t.me/s/{src['channel']}",
                 source=src["name"], published=pub, lang=src.get("lang", "en"))
        )
    return out


FETCHERS = {
    "rss": fetch_rss,
    "gnews": fetch_gnews,
    "shmet": fetch_shmet,
    "cls": fetch_cls,
    "wscn": fetch_wscn,
    "jin10": fetch_jin10,
    "telegram": fetch_telegram,
}


def fetch_all(sources: list[dict], lookback_hours: int) -> tuple[list[Item], list[str]]:
    """返回 (时间窗内的条目, 失败源的说明)。"""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    items: list[Item] = []
    failures: list[str] = []

    for src in sources:
        fetcher = FETCHERS.get(src["type"])
        if fetcher is None:
            failures.append(f"{src['name']}: 未知源类型 {src['type']}")
            continue
        try:
            got = fetcher(src)
        except Exception as e:  # 单源失败不影响整轮
            failures.append(f"{src['name']}: {type(e).__name__}: {e}")
            log.warning("源 %s 抓取失败: %s", src["name"], e)
            continue

        fresh = [i for i in got if i.published >= cutoff and i.title]
        log.info("源 %-18s 抓到 %3d 条，窗口内 %3d 条", src["name"], len(got), len(fresh))
        items.extend(fresh)

    items.sort(key=lambda i: i.published, reverse=True)
    return items, failures
