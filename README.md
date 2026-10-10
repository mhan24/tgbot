# Telegram 多群管理机器人

使用 Python 3.9+ 标准库与 SQLite，实现入群验证、成员管理、积分、空投和 AI 问答。多个群同时运行，各群业务数据独立保存。

## 功能

| 功能 | 使用方式与规则 |
|---|---|
| 入群验证 | 检查关联频道关注状态、Bot 身份与黑名单，普通成员完成算术验证 |
| 成员管理 | `/manage` 按钮处理警告、禁言、拉黑、解封和白名单；累计 3 次警告拉黑并请求删除该成员全部群消息 |
| 广告举报 | 回复消息发送 `/report`，Jev 判断后由 1 名管理员或 5 名群员确认；确认后删除广告并警告一次 |
| 积分 | `/checkin` 每日签到，默认随机 1–5 分；发言默认每条 1 分、每日最多 5 分；头像须对机器人可见 |
| 榜单 | 私聊 `/points`、`/rank`、`/milestones`，按群查询；积分榜每页 50 人，原神榜展示首次达标前 50 名 |
| 空投 | `/airdrop` 设置积分、活跃门槛与中奖人数；成员申请需审批；使用 Base 区块证明，`/airdrops` 查询历史 |
| AI 问答 | `/ai 问题` 或回复消息发送 `/ai`，支持回复链、图片及已记录相册；显示模型 ID，思考时添加 🤔 反应 |
| 上下文筛选 | Jev 判断外围群聊是否相关；直接引用和回复链保留，不确定或筛选失败时保留背景 |
| 汇率换算 | `/rate 100 USD CNY`，或 `/rate USD CNY`；小时缓存，显示数据更新时间 |
| BIN 查询 | `/bin 45717360`，查询 6–8 位前缀 |
| 多群配置 | `/group_add` 登记，私聊 `/groups` 启停/选择群；群主通过 `/settings` 按钮修改各群参数 |
| 解封与审计 | 私聊 `/appeal` 提交理由，由群主处理；`/audit` 无参数查看最近 100 条操作日志 |

Telegram 菜单使用英文命令、中文说明。普通文字 `签到`、`积分`、`排行榜`、`原神榜` 也可触发对应功能，不支持中文斜杠命令。完整入口见 [命令与权限](docs/COMMANDS.md)。

## 消息与权限规则

- 用户源消息保留，普通群内机器人回复默认 30 秒删除；**AI 回答固定 10 分钟删除**。私聊不参与群自动清理。
- 验证、投票、审批和管理面板保留到流程结束，再按规则清理；关闭管理面板立即删除。
- 群内操作没有每日总次数上限。签到每日一次，空投申请额度和普通用户 AI 冷却独立生效；AI 默认冷却 60 秒，管理员与白名单豁免。
- 广告仅通过 `/report` 手动提交，没有自动扫描；Jev 上下文相关性筛选不产生处罚。
- 普通成员头像不可见时，发言不计分；签到则提示并计一次警告。完整三天仅签到、没有正常发言会拉黑并清零积分，管理员与白名单豁免。
- 管理按钮重新核验所属群、操作者和当前权限。群主配置、积分、名单、历史和活动按群隔离。

## 快速启动

无需安装第三方 Python 包。

```sh
cp .env.example .env
chmod 600 .env
# 编辑 .env，填写 TELEGRAM_BOT_TOKEN、GROUP_ID、CHANNEL_ID
python3 -m unittest discover -s tests -q
python3 bot.py
```

运行前将机器人设为群和关联频道的管理员，授予删除消息、限制成员、邀请成员等权限；成员标签需要 Telegram 对应管理权限。不要同时运行多个轮询同一 token 的实例。生产部署参考 [部署文档](docs/DEPLOYMENT.md) 和 [systemd 服务](tgbot.service)。

### 可选服务

| 配置 | 用途 |
|---|---|
| `AI_BASE_URL`、`AI_API_KEY`、`AI_MODEL` | OpenAI 兼容问答接口，模板模型为 `sensenova-6.8-flash-lite` |
| `JEV_API_KEY`、`JEV_MODEL` | 广告判断及 AI 上下文相关性筛选；广告开关不影响相关性筛选 |
| ExchangeRate.fun、Binlist | 汇率与 BIN 公共查询接口，无需额外密钥 |

AI 模型需支持相应输入类型；图片能力取决于上游模型。机器人只使用已收到或已记录的信息，不能恢复未保存的历史群聊，也不能通过 Bot API 枚举所有历史成员或读取最后在线时间。

## 代码导航

- [bot.py](bot.py)：Telegram 接口、群分发、配置、验证、命令和持久化删除队列。
- [ai_chat.py](ai_chat.py)、[history.py](history.py)、[vision.py](vision.py)、[context_filter.py](context_filter.py)：问答、历史、媒体和相关性筛选。
- [moderation.py](moderation.py)、[jev_ads.py](jev_ads.py)、[appeals.py](appeals.py)：管理、举报及解封。
- [points.py](points.py)、[milestones.py](milestones.py)、[attention.py](attention.py)：积分、达标与异常签到处理。
- [airdrop.py](airdrop.py)、[chain.py](chain.py)：空投与链上证明。
- [exchange.py](exchange.py)、[binlookup.py](binlookup.py)：汇率与 BIN 查询。
- [scripts](scripts)：指令菜单与说明维护；[tests](tests)：回归测试。

## 文档与维护

| 文档 | 内容 |
|---|---|
| [功能规则](docs/FEATURES.md) | 各功能的完整行为、默认值和限制 |
| [命令与权限](docs/COMMANDS.md) | 当前命令、按钮和使用示例 |
| [代码结构](docs/ARCHITECTURE.md) | 模块职责、数据隔离、队列与维护 |
| [配置与部署](docs/DEPLOYMENT.md) | 环境变量、systemd、备份与排查 |

修改功能时同步文档和相关测试；菜单变更后运行 `python3 scripts/update_commands.py`，该脚本更新已有 Telegram 菜单，不发送群消息。

`.env`、密钥、SQLite 数据、Telegram 会话和运行日志不提交。`.env.example` 只提供空凭据模板。备份应包含各群 SQLite 数据库，备份密钥与业务数据分开存放。
