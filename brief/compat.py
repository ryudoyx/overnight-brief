#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""OpenAI 兼容后端——用来接各家的免费额度。

Gemini / Groq / 智谱 / DeepSeek / OpenRouter 都提供 OpenAI 兼容的
/chat/completions 端点，一份代码换个 base_url + model 就能切。只用 requests。

走的是 JSON mode（response_format=json_object）而不是严格 schema：各家对
json_schema 的支持参差不齐，JSON mode 才是最大公约数。输出仍然过 pydantic 校验，
校验失败就整批丢掉，不会把脏数据混进页面。
"""
import os
import re
import json
import time

import requests
from pydantic import ValidationError

from .news import BUCKET_SPEC, _render

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)

SCHEMA_HINT = """
只输出 JSON，不要 markdown 代码块，不要任何解释文字。格式：
{"items":[{"id":0,"relevant":true,"stale":false,"importance":3,"category":"...",
"direction":"利多","title_zh":"...","summary_zh":"..."}]}

category 只能是: %s
direction 只能是: 利多 / 利空 / 中性
importance 是 1 到 5 的整数
stale 是布尔值：只有当这是几年前的旧文章被重新推送时才填 true；
月度数据在次月公布（如 8 月发布 7 月海关数据）属正常新闻，填 false
每条输入都要有对应 id，不要漏，不要编造。
"""

CATS = {
    "宏观": "货币政策 / 经济数据 / 财政与政治 / 关税与贸易 / 地缘与能源 / 公司与行业 / 其他",
    "铜": "供应 / 需求 / 库存 / 宏观政策 / 价格交易 / 公司项目 / 其他",
    "铝": "供应 / 需求 / 库存 / 宏观政策 / 价格交易 / 公司项目 / 其他",
}


class CompatJudge:
    def __init__(self, cfg):
        self.base_url = cfg["base_url"].rstrip("/")
        self.model = cfg["model"]
        key_env = cfg.get("api_key_env", "LLM_API_KEY")
        self.api_key = os.environ.get(key_env, "")
        if not self.api_key:
            raise RuntimeError(
                f"环境变量 {key_env} 没设置。config.yaml 里 judge.backend 是 "
                f"openai_compat，需要这个 key。免费额度去 aistudio.google.com 领。")
        self.temperature = cfg.get("temperature", 0.2)
        self.retries = cfg.get("retries", 4)
        # 免费层的 503「模型过载」是常态而非异常：实测 gemini-3.7-flash 三次里挂一次。
        # 所以既要重试，也要备胎——降级链上的 lite 模型实测三次全过。
        self.chain = [self.model] + list(cfg.get("fallback_models") or [])
        self.usage = {"input": 0, "output": 0}

    def chat(self, system, user, max_tokens=8000):
        """裸调用，判定和串讲共用。先在当前模型上重试，仍不行就降级到下一个。"""
        body = {
            "temperature": self.temperature,
            "max_tokens": max_tokens,
            "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
        }
        last = None
        for model in self.chain:
            for attempt in range(self.retries):
                try:
                    r = requests.post(
                        f"{self.base_url}/chat/completions",
                        headers={"Authorization": f"Bearer {self.api_key}"},
                        json={**body, "model": model}, timeout=120)
                except requests.RequestException as e:
                    last = f"{type(e).__name__}"
                else:
                    if r.status_code == 200:
                        d = r.json()
                        u = d.get("usage") or {}
                        self.usage["input"] += u.get("prompt_tokens", 0)
                        self.usage["output"] += u.get("completion_tokens", 0)
                        self.served_by = model
                        return _FENCE.sub("", d["choices"][0]["message"]["content"]).strip()
                    last = f"HTTP {r.status_code}"
                    # 404 = 这个账号根本调不到这个模型（列表里有不代表能用），
                    # 重试多少次都没用，直接换下一个
                    if r.status_code == 404:
                        break
                    # 429 是免费额度用完，跟 503 一样等一下再来，不会偷偷扣钱
                    if r.status_code not in (429, 500, 502, 503, 504):
                        break
                # 免费层速率限制要等够：1/4/8/16 秒，比 2^n 更耐撞
                time.sleep(4 ** attempt / 4)
            if model != self.chain[-1]:
                print(f"  {model} 不行（{last}），降级到下一个模型")
        raise RuntimeError(f"降级链走完仍失败，最后一次：{last}")

    def judge(self, bucket, items, batch_size=20):
        system, schema_model = BUCKET_SPEC[bucket]
        system = system + SCHEMA_HINT % CATS[bucket]
        paired = []
        for start in range(0, len(items), batch_size):
            chunk = items[start:start + batch_size]
            try:
                import datetime as _dt
                today = _dt.datetime.now(_dt.timezone(_dt.timedelta(hours=8)))
                text = self.chat(
                    system,
                    f"今天是 {today:%Y年%m月%d日}。\n"
                    f"以下是 {len(chunk)} 条待判断的隔夜新闻，请逐条给出结论：\n\n" + _render(chunk))
                got = schema_model.model_validate_json(text).items
            except (requests.RequestException, ValidationError,
                    json.JSONDecodeError, KeyError, RuntimeError) as e:
                print(f"  这一批判定失败，跳过：{type(e).__name__}: {str(e)[:100]}")
                continue
            for j in got:
                if 0 <= j.id < len(chunk):
                    paired.append((chunk[j.id], j.model_dump(exclude={"id"})))
        return paired

    def cost_usd(self):
        return 0.0      # 免费额度内
