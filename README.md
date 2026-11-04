# Telegram 入群验证机器人

从零重构的版本，使用 Python 3.9+ 标准库与 SQLite。当前只实现**功能一：入群验证系统**；原石系统（功能二）和抽卡系统（功能三）尚未实现，代码中没有对应入口。

## 现有功能

| 功能 | 规则 |
|---|---|
| 入群申请验证 | 申请人在私聊收到验证卡片：先关注关联频道，再点对算术题的正确答案；通过后自动批准入群 |
| 直接入群验证 | 被邀请直接进群的普通成员先被禁言，在群内完成同样的验证后恢复发言；超时或答错 3 次会被移出 |
| 放行 | 群主和管理员直接放行；Bot 只接受管理员邀请；黑名单用户拒绝入群 |
| 提示 | `/help` 在群和私聊说明验证步骤与规则，`/start` 私聊说明入群流程 |

验证规则：算术题为两位数加法四选一；有效期由 `VERIFY_SECONDS` 决定（默认 240 秒）；最多答错 3 次。未关注频道时点击答案只提示先关注，不消耗答错次数，也不会通过验证。验证卡片在验证期间保留，结束（通过、答错上限、超时）后按群自动清理时长删除。

## 快速启动

无需安装第三方 Python 包。

```sh
cp .env.example .env
chmod 600 .env
# 编辑 .env：填写 TELEGRAM_BOT_TOKEN、GROUP_ID、CHANNEL_ID
python3 -m unittest discover -s tests -q
python3 bot.py
```

运行前把机器人设为群和关联频道的管理员，群内需要删除消息、限制成员、邀请成员权限。群应开启“入群申请”，否则成员会直接进群走禁言验证流程。不要同时运行多个轮询同一 token 的实例。

### 配置

| 环境变量 | 用途 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 必填，机器人 token |
| `GROUP_ID` / `CHANNEL_ID` | 必填，默认群与验证频道的数字 ID |
| `TELEGRAM_CHANNEL` / `TELEGRAM_CHANNEL_URL` | 验证卡片的“关注频道”按钮链接，可填 `@频道用户名` 或 t.me 链接；留空时不显示该按钮 |
| `VERIFY_SECONDS` | 验证有效期，30–300 秒，默认 240 |
| `DELETE_AFTER_SECONDS` | 群内机器人消息自动清理秒数，0 关闭，默认 30 |
| `DB_PATH` | SQLite 路径，默认 `bot.sqlite3` |

所有参数来自 `.env` / 环境变量，修改后需重启服务；机器人内没有配置命令。

## 代码导航

- [bot.py](bot.py)：配置、Telegram 调用、单群过滤、消息生命周期、更新分发与长轮询。
- [verification.py](verification.py)：验证状态机（发题、判题、通过、超时/答错拒绝、投递与恢复重试）。
- [telegram.py](telegram.py)：Bot API 传输层，区分可重试与永久失败。
- [retention.py](retention.py)：失败与处罚通知的显式保留时长。
- [scripts](scripts)：命令菜单说明维护；[tests](tests)：使用模拟 Telegram 与内存 SQLite 的回归测试。

## 文档与维护

| 文档 | 内容 |
|---|---|
| [开发约束](AGENTS.md) | 开发范围、权限、数据、测试与部署要求 |
| [功能规则](docs/FEATURES.md) | 验证流程、放行条件、限制和未实现项 |
| [命令与权限](docs/COMMANDS.md) | 当前命令、按钮与权限 |
| [代码结构](docs/ARCHITECTURE.md) | 模块职责、表结构、重试与数据隔离 |
| [消息保留规范](docs/MESSAGE_RETENTION.md) | 消息生命周期要求与接入检查 |
| [配置与部署](docs/DEPLOYMENT.md) | 环境变量、systemd、备份与排查 |

`.env`、密钥、SQLite 数据和运行日志不提交。菜单说明变化后运行 `python3 scripts/update_commands.py`；该脚本只刷新已有菜单，不是从零初始化工具。

## 测试

```sh
python3 -m unittest discover -s tests -q
```

测试使用模拟 Telegram API 与内存 SQLite，覆盖频道校验顺序、答错次数、超时拒绝、重复入群、直接入群禁言与恢复、群隔离、投递失败重试、卡片清理与消息保留时长。
