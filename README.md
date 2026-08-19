# overnight-brief · 隔夜早报

每天北京时间 08:00 自动生成一份隔夜市场快照，推到 GitHub Pages。
前一晚发生了什么宏观大事、美股哪些板块和个股在动、金银铜油怎么走、铜的基本面消息——
一页看完。

```
GitHub Actions (00:00 UTC)
        │
   ┌────┴──────────┐
 行情 quotes.py    新闻 news.py
   ├ 指数/VIX/利率汇率   ├ 14 个源抓取
   ├ 11 个 SPDR 板块     ├ 去重 + 分桶粗筛
   ├ 个股异动扫描        └ 判定打分（三档后端）
   └ 商品(单位统一)
        └──────┬───────┘
          事实包 JSON  ← 所有数字都在这一步算完
               │
          narrate.py   ← LLM 只写字，不算数
               │
        单文件 HTML → GitHub Pages + archive/
```

## 一条铁律

**页面上的每个数字都由代码算出，LLM 一个都不许碰。** 它只负责挑新闻、写摘要、
写那段串讲。系统提示里明令禁止它做任何算术，实测过它确实守规矩。

## 三档判定后端

改 `config.yaml` 里 `judge.backend` 一行切换：

| 档位 | 要 key | 中文摘要 | 串讲 | 成本 |
|---|---|---|---|---|
| `rules` | 不要 | ✗ 英文标题保留原文 | ✗ | 0 |
| `openai_compat` | Gemini 免费额度 | ✓ | ✓ | 0 |
| `claude` | 要付费 | ✓ 最好 | ✓ | ~$5/月 |

`openai_compat` 走各家的 OpenAI 兼容端点，换 base_url + model 就能切到
Groq / 智谱 / DeepSeek。免费层的 503「模型过载」是常态，所以配了重试和
自动降级链（`fallback_models`）。

## 本地跑

```bash
python run.py                 # 正常跑
python run.py --no-llm        # 跳过 LLM，页面照出
python run.py --quotes-only   # 只打印行情事实包，调数据源时用
python run.py --rebuild DATE  # 用存档重出页面，调样式时不重新取数
```

## 踩过的坑

- **Yahoo 日线在早上还没结算**：最近一根 bar 的 close 是 null，真收盘价只在
  `meta.regularMarketPrice` 里。直接 dropna 会拿到两天前的数据，而 8:40 恰好
  每天都撞上这个空窗。现在按交易日定位前一场，缺口用小时线回填。
- **各标的的「当前是哪一场」不一致**：VIX、期货有夜盘，`regularMarketTime`
  比正股晚一天。逐个判断，`span_days` 字段记录实际跨了几天。
- **`HG=F` 返回 USD/lb**，不是美分/磅也不是美元/吨。换算因子写死在 `CONVERT` 里。
- **Yahoo 对普通 requests 稳定 429**，必须走 curl_cffi 指纹伪装。
- **Gemini `/models` 列出的不等于能调**：`gemini-2.5-flash` 在列表里但直接 404。
- **同一件事换四种措辞**，字面去重堵不住，得按分类限流保版面多样性。

## 配置

标的池、新闻源、关键词表、阈值全在 `config.yaml`，加减不用动代码。
