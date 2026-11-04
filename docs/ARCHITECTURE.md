# 代码结构与维护

核对日期：2026-11-04。项目只依赖 Python 标准库，不需要 pip 安装第三方库。当前版本是从零重构的第一阶段，只包含入群验证；功能二（原石）与功能三（抽卡）尚未接入。

## 模块职责

| 模块 | 职责 |
|---|---|
| `bot.py` | 配置读取、Telegram 调用封装、单群过滤、消息删除队列、更新分发、`/start`、`/help`、权限与成员状态、长轮询主循环 |
| `verification.py` | 验证状态机：发题、判题、批准或拒绝、超时与答错处理、投递和恢复重试、卡片登记 |
| `telegram.py` | Bot API 传输层：请求、响应解析、`APIError`（`code`、`description`、`retry_after`、`permanent`） |
| `retention.py` | 失败与处罚通知的显式保留时长常量 |
| `scripts/command_descriptions.json` | 菜单里保留的英文命令及中文说明 |
| `scripts/update_commands.py` | 刷新已有菜单说明，移除不在命令表里的条目 |
| `tests/` | 模拟 Telegram API 与内存 SQLite 的回归测试 |

## 更新处理

```text
Telegram getUpdates
  → 控制层：只接受默认群与用户私聊
  → 分发：callback_query / chat_join_request / chat_member / message
  → 验证服务处理入群与答题
  → 后台维护：验证超时与重试、卡片清理、删除队列
  → 保存处理位置，继续长轮询
```

`Bot.call()` 统一发送请求：群内 `sendMessage` 默认进入删除队列（`keep=True` 暂缓），`banChatMember` 强制携带 `revoke_messages`，`answerCallbackQuery` 的“query is too old”视为已处理。`Bot.send()` 是唯一的群内发送入口，默认附带 HTML 解析模式与清理时长。

## 数据

单群部署，一个 SQLite 数据库（默认 `bot.sqlite3`，systemd 使用 `/var/lib/tgbot/`）。

| 表 | 用途 |
|---|---|
| `meta` | 处理位置 `offset`、启动时间 |
| `processed` | 已处理更新 ID，防止重复处理 |
| `audit` | 操作与判决记录（谁对谁做了什么） |
| `blacklist` | 历史黑名单，验证时只读；当前版本没有新增入口 |
| `deletions` | 待删除群消息队列（重试次数、到期时间） |
| `verification` | 每人一行验证状态：题型、题目、答案、随机串、有效期、答错次数、`state`、投递与恢复重试字段 |
| `verification_prompts` | 群内验证卡片的消息 ID，与验证随机串绑定，结束后转入删除队列 |

数据库采用 WAL。备份应使用 SQLite backup API 或停服后完整复制，避免只复制正在写入的主文件。

**不做迁移、不做兼容**：初始化只创建当前完整结构，代码里没有 `ALTER TABLE`、旧字段读取或数据搬迁分支。如果现有 `verification` 表结构与 `Verification.REQUIRED_COLUMNS` 不一致，构造 `Bot` 会直接抛 `RuntimeError` 并提示删除 `verification`、`verification_prompts` 后重启——不会静默改表，也不会尝试兼容旧行。旧版其他遗留表（积分、签到、举报等）不再创建、不再读取，也不由代码删除。

## 状态与重试

- 验证卡片发送失败：非永久错误每 30 秒重试，最多 3 次；永久错误（400/403）或次数用尽则结束验证并按失败处理（拒绝申请或保留禁言）。
- 拒绝申请、移出成员失败：若不是“用户已不存在”“申请已撤销”这类已终结错误，保留记录并每 30 秒重试；权限恢复后自动完成。
- 恢复发言失败：保留通过状态，后台每 30 秒重试，最多 5 次。
- 删除消息失败：400/403 直接放弃；429 按 `retry_after` 延后；其他错误重试 2 次后放弃并写审计。
- 维护任务分别捕获异常并冷却 30 秒重试，单项失败不影响其他任务。
- 通过状态先落库再调用 `approveChatJoinRequest`，保证伴随入群更新不会重复发题。

Telegram 与 SQLite 无法构成同一事务，因此代码保存可恢复状态、区分“业务未执行”和“已执行但消息未送达”，不承诺外部调用严格只执行一次。

## 维护要求

开发约束统一维护于 [AGENTS.md](../AGENTS.md)；消息时长与接入规范见 [MESSAGE_RETENTION.md](MESSAGE_RETENTION.md)。新增功能应作为独立模块接入 `bot.py` 的分发层，不要在分发层写业务规则。

运行、测试和恢复方法见 [DEPLOYMENT.md](DEPLOYMENT.md)。
