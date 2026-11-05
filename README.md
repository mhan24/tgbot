# Telegram 入群验证机器人

Python 3.9+ 标准库 + SQLite 的单群机器人：入群验证、汇率换算、卡 BIN 查询。机器人不自动删除任何消息。

## 现有功能

| 功能 | 规则 |
|---|---|
| 入群申请验证 | 申请人在私聊收到验证卡片：先关注关联频道，再点对算术题的正确答案；通过后自动批准入群 |
| 直接入群验证 | 被邀请直接进群的普通成员先被禁言，在群内完成同样的验证后恢复发言；超时或答错 3 次会被移出 |
| 放行 | 群主和管理员直接放行；Bot 只接受管理员或群主邀请，其他 Bot 立即封禁 |
| 提示 | `/help` 在群和私聊说明验证步骤与规则，`/start` 私聊说明入群流程 |
| 汇率换算 | `/rate 100 USD CNY`，或 `/rate USD CNY`；exchangerate.fun 免钥接口，USD 基准缓存 1 小时，显示数据时间 |
| 卡 BIN 查询 | `/bin 45717360`（6–8 位数字）；cardbincheck.com（IPNOVA）接口，密钥可选，成功结果缓存 24 小时，缺失字段显示未知 |
| 消息保留 | 机器人不自动删除任何消息；验证卡片结束只改文案；唯一删除场景是处罚移出成员时撤回其发言 |

验证规则：算术题为两位数加法四选一；有效期由 `VERIFY_SECONDS` 决定（默认 240 秒）；最多答错 3 次。未关注频道时点击答案只提示先关注，不消耗答错次数，也不会通过验证。完整规则见[功能与命令](docs/FEATURES.md)。

## 快速启动

无需安装第三方 Python 包。

```sh
cp .env.example .env
chmod 600 .env
# 编辑 .env：填写 TELEGRAM_BOT_TOKEN、GROUP_ID、CHANNEL_ID
python3 -m unittest discover -s tests -q
python3 bot.py
```

运行前把机器人设为群和关联频道的管理员：频道用于读取成员状态，群内用于限制成员、邀请成员和撤回消息。群应开启“入群申请”，否则成员会直接进群走禁言验证流程。不要同时运行多个轮询同一 token 的实例。

## 配置

必填 `TELEGRAM_BOT_TOKEN`、`GROUP_ID`、`CHANNEL_ID`；常用可选项是验证卡片链接 `TELEGRAM_CHANNEL`、有效期 `VERIFY_SECONDS` 和 BIN 查询密钥 `BIN_API_KEY`。全部环境变量及默认值只维护在[配置与部署](docs/DEPLOYMENT.md#1-运行条件)，这里不重复。

所有参数来自 `.env` / 环境变量，修改后需重启服务；机器人内没有配置命令。

## 代码结构

| 模块 | 职责 |
|---|---|
| `bot.py` | 配置读取、Telegram 调用封装、单群过滤、更新分发、`/start`、`/help`、`/rate`、`/bin`、权限与成员状态、长轮询主循环 |
| `verification.py` | 验证状态机：发题、判题、批准或拒绝、超时与答错处理、投递和恢复重试 |
| `exchange.py` | `/rate` 汇率换算：exchangerate.fun 拉取、USD 基准 1 小时缓存、Decimal 交叉换算、参数与币种校验 |
| `binlookup.py` | `/bin` 卡 BIN 查询：cardbincheck.com 拉取、可选鉴权、成功结果 24 小时缓存、字段映射与缺失兜底 |
| `botapi.py` | Bot API 传输层：请求、响应解析、`APIError`（`code`、`description`、`retry_after`、`permanent`） |
| `scripts/` | 命令菜单维护：`command_descriptions.json` 与 `update_commands.py` |
| `tests/` | 模拟 Telegram API 与内存 SQLite 的回归测试 |

```text
getUpdates → 只接受默认群与用户私聊 → 分发（callback_query / chat_join_request / chat_member / message）
  → 验证服务处理入群与答题；/rate、/bin 在命令线程内同步处理（10 秒超时）
  → 后台维护：验证超时与重试、更新 ID 修剪
  → 保存处理位置，继续长轮询
```

`Bot.call()` 统一发送请求：`banChatMember` 强制携带 `revoke_messages`，`answerCallbackQuery` 的“query is too old”视为已处理。`Bot.send()` 是唯一发送入口，默认 HTML 解析模式，不设置删除任务。

## 数据

单群部署，一个 SQLite 数据库（默认 `bot.sqlite3`，systemd 使用 `/var/lib/tgbot/`），WAL 模式，共 6 张表。

| 表 | 用途 |
|---|---|
| `meta` | 处理位置 `offset`、启动时间 |
| `processed` | 已处理更新 ID，防止重复处理 |
| `audit` | 操作与判决记录（谁对谁做了什么） |
| `verification` | 每人一行验证状态：题目、答案、随机串、有效期、答错次数、`state`、投递与恢复重试字段 |
| `rates` | 汇率缓存：USD 基准的完整汇率表、上游数据时间与拉取时间 |
| `bin_cache` | BIN 缓存：已成功查询的原始 JSON，24 小时内复用 |

表结构只由当前代码创建，没有 `ALTER TABLE` 或旧数据搬迁分支；`verification` 表结构不符时 `Bot` 直接抛 `RuntimeError` 并提示删除该表后重启。旧版残留的 `deletions`、`verification_prompts` 已无代码读写，可以 `DROP`。

## 外部服务

| 接口 | 用途 |
|---|---|
| `api.exchangerate.fun/latest` | 汇率数据，免费免钥，每小时更新 |
| `cardbincheck.com/api/bin/{bin}` | BIN 数据（IPNOVA），`BIN_API_KEY` 可选：配置后按额度计费，未配置按 IP 限流；成功结果缓存 24 小时 |

两个上游在命令处理线程内同步调用（10 秒超时）；失败有明确提示，不使用过期缓存伪造结果。

## 文档与维护

| 文档 | 内容 |
|---|---|
| [开发约束](AGENTS.md) | 开发范围、权限、数据、消息保留规则、测试与部署要求 |
| [功能与命令](docs/FEATURES.md) | 验证流程、命令与按钮、权限、失败恢复、限制 |
| [配置与部署](docs/DEPLOYMENT.md) | 环境变量、systemd、升级、备份与排查 |

`.env`、密钥、SQLite 数据和运行日志不提交。菜单文案变化后运行 `python3 scripts/update_commands.py`；该脚本只刷新已有菜单，不是从零初始化工具。

## 测试

```sh
python3 -m unittest discover -s tests -q
```

测试使用模拟 Telegram API 与内存 SQLite，覆盖频道校验顺序、答错次数、超时拒绝、重复入群、直接入群禁言与恢复、群隔离、投递失败重试、卡片保留与不删除消息。
