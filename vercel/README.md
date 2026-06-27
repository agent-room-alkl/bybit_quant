# bybit-quant on Vercel

把交易 bot 从「本地常驻进程」改造成「Vercel Cron + Serverless」，策略逻辑与本地完全一致。
状态全部存 Supabase Postgres 的 `bybit_bot` schema（与本地共用同一库，隔离 schema）。

## 架构

```
Vercel Cron (每分钟)
   └─ GET /api/cron/tick   ← 跑「一拍」：读行情→算信号→(可选)下单→写PG
                              · 执行锁(owner+TTL) 防重叠重复下单
                              · 新闻情绪 PG 缓存(TTL 45min)，过期才调 GPT
GET /api/status            ← 只读看板/健康检查(轻量, 无pandas)
```

策略大脑 = `lib/` 下的 `strategy_v5 / bot_core / cost / indicators / trade_logic / bybit_client / news_sentiment`（从本地 prd 原样搬来）。
唯一改动：存储层换成 `lib/db_pg.py`（PG 版，函数签名与原 `db.py` 一致），`tick.py` 用 `sys.modules['db']=db_pg` 让策略代码零改动。

## 部署步骤

1. 把本目录推到一个 Git 仓库，在 Vercel 导入该项目（Framework Preset 选 **Other**）。
2. 在 Vercel → Settings → Environment Variables 添加（Production + Preview 都勾）：

   | 变量 | 说明 |
   |------|------|
   | `POSTGRES_PRISMA_URL` | Supabase 池化连接串(6543)，从本地 .env 复制 |
   | `BYBIT_API_KEY` | Bybit API key |
   | `BYBIT_API_SECRET` | Bybit API secret |
   | `GPT_API_KEY` | OpenAI key（新闻情绪用） |
   | `CRON_SECRET` | 自设随机串，**必填**，否则 tick 拒绝执行 |
   | `ENABLE_TRADING` | **首发填 `false`** 跑影子；验证无误后改 `true` 才真实下单 |
   | `STATUS_SECRET` | 可选。设了之后 `/api/status` 要带 `?key=` 才回交易明细；不设则只公开 counts |

3. Deploy。Vercel 会读 `vercel.json` 自动注册每分钟 cron（需 **Pro** 套餐支持分钟级）。
   - Vercel Cron 调 `/api/cron/tick` 时会自动带 `Authorization: Bearer $CRON_SECRET`。

## 上线验证（影子模式）

- 部署后等几分钟，打开 `https://<域名>/api/status`：
  - `counts.signals` 应每分钟 +1 → 说明 cron 在跑、在写 PG。
  - `last_signal` 是最近一拍的决策。
- 确认无重复 tick、决策符合预期后，把 `ENABLE_TRADING` 改成 `true` 再 Redeploy，开始真实交易。

## 安全默认

- `lib/config.json` 里 `enable_trading=false`：不显式配 `ENABLE_TRADING=true` 就只跑影子、不下单。
- `/api/cron/tick` 没配 `CRON_SECRET` 直接拒绝（fail-closed）。
- 执行锁异常时不放行（fail-closed），宁可漏一拍也不重复下单。

## 已知待办

- PG 的 `trades/signals` 会持续增长（本地原是 200/500 环形缓冲）。建议加保留策略：定期只留最近 N 条或按天归档。
- pandas+numpy 接近 Vercel 250MB 解压上限，是目前最紧的约束；若构建因体积失败，可考虑精简 indicators 的 pandas 依赖或改用容器部署。
