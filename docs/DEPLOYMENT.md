# 配置、部署与维护

核对日期：2026-10-05。生产路径由仓库 `tgbot.service` 定义；可按实际主机调整。

## 1. 运行条件与凭据

Python 3.9+，仅标准库。运行用户需要读配置、读写数据库，并能连接 Telegram、Jev、AI、BIN 和 Base 区块来源。

复制 `.env.example` 为 `.env` 并设置权限 `600`。环境变量优先于文件配置。不要在 Git、日志或文档中保存真实 token、API key、SSH 密码。

| 环境变量 | 用途/默认值 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 必填，机器人 token |
| `GROUP_ID` / `CHANNEL_ID` | 初始管理群和验证频道的数字 ID |
| `TELEGRAM_GROUP` / `TELEGRAM_CHANNEL` | 部署相关名称配置，不能替代数字 ID；新群可独立绑定频道 |
| `DB_PATH` | 默认 `bot.sqlite3`；systemd 覆盖为 `/var/lib/tgbot/bot.sqlite3` |
| `JEV_API_KEY` | 手动举报所用 Jev 凭据 |
| `JEV_MODEL` / `JEV_ENABLED` | 默认 `jev-latest` / `1` |
| `AI_BASE_URL` / `AI_API_KEY` | AI 的 OpenAI 兼容接口和凭据 |
| `AI_MODEL` | 模板默认 `grok-4.7`；不开放群主修改 |
| `CHECKIN_MIN` / `CHECKIN_MAX` | 默认 `1` / `5` |
| `MESSAGE_POINTS` / `MESSAGE_DAILY_LIMIT` | 默认 `1` / `5` |
| `MUTE_SECONDS` / `VERIFY_SECONDS` | 默认 `86400` / `240` 秒 |
| `DELETE_AFTER_SECONDS` | 默认 `30` 秒；`0` 关闭自动清理 |
| `AI_COOLDOWN_SECONDS` | 默认 `60` 秒；`0` 关闭普通成员冷却 |
| `HISTORY_MESSAGES` / `HISTORY_CONTEXT` | 默认 `50` / `50` 条 |
| `UPGRADE_POINTS` / `UPGRADE_TAG` | 默认 `50` / `家人` |
| `SSH_HOST` / `SSH_PORT` / `SSH_USER` / `SSH_PASSWORD` | 可选本地部署连接信息，机器人运行不使用；不要复制到服务器运行配置 |

每日群内机器人操作次数限制已移除，无对应运行参数。

## 2. 群权限与登记

机器人应为每个管理群和关联频道的管理员。群内需要删消息、限制成员及入群申请相关权限；设置成员标签还需 `can_manage_tags`。将目标群配置为需要审批的邀请途径。

群主在目标群发送 `/group_add [@频道或频道ID]`，再私聊 `/groups` 启用。通过 `/settings` 为各群设置独立频道和邀请链接。

没有处理位置的首次启动会跳过积压更新；启动检测到已有 webhook 时拒绝替换，应先由维护人员确认该 token 的使用方式。不要启动两个同时轮询该 token 的实例。

## 3. 群主按钮配置

群主私聊 `/settings`，选群、选项，再按普通消息提示直接发新值。无需引用回复，也不用弹窗。待输入有效期 10 分钟，`/cancel` 取消；开关直接按钮切换，覆盖值可恢复默认。

| 配置项 | 合法范围 |
|---|---|
| 签到积分范围 | 下限/上限各 1–1000，下限不能超过上限；一次输入如 `5-10` |
| 每条发言积分 | 1–100 |
| 每日发言积分上限 | 0–1000 |
| 默认临时禁言 | 60–2592000 秒 |
| 入群验证时长 | 30–300 秒 |
| 群内自动删除 | 0–3600 秒，0 关闭 |
| AI 历史保留条数 | 0–500 |
| 每次 AI 上下文条数 | 0–200 |
| Jev 举报 | 开/关 |
| 达标积分门槛 | 1–1000000 |
| 成员标签 | 最多 16 个 UTF-16 单位，不允许 Emoji |
| AI 冷却 | 0–86400 秒，0 关闭 |
| 验证频道、频道邀请链接 | 本群频道用户名/数字 ID；对应 Telegram 链接 |

群主不能在设置中修改机器人 token、API key、AI 地址、模型 ID 或数据库路径。群配置持久保存、立即生效并覆盖部署默认值；改 `.env` 默认值需要重启，不会覆盖各群已保存的自定义值。

## 4. systemd 部署示例

以下由主机管理员执行。先把代码放到 `/opt/tgbot`，并填写该目录的 `.env`。

```sh
# 仅首次部署创建服务用户；已存在时跳过
useradd --system --no-create-home --shell /usr/sbin/nologin tgbot
install -d -o tgbot -g tgbot -m 700 /var/lib/tgbot
chown root:tgbot /opt/tgbot/.env
chmod 640 /opt/tgbot/.env
install -m 644 /opt/tgbot/tgbot.service /etc/systemd/system/tgbot.service
systemctl daemon-reload
systemctl enable --now tgbot
systemctl status tgbot --no-pager
```

源码目录需对 `tgbot` 可读。配置归属与 `640` 示例用于让专用服务用户读取；本地只供当前用户读取时使用 `600`。服务使用 `UMask=0077`、只读系统目录和专用 StateDirectory，数据库不能放在被只读保护的源码目录。

## 5. 验证与更新

```sh
cd /opt/tgbot
python3 -m unittest discover -s tests -v
systemctl restart tgbot
systemctl is-active tgbot
journalctl -u tgbot -n 50 --no-pager
python3 scripts/update_commands.py
```

菜单脚本使用 `/opt/tgbot/.env` 与 `/var/lib/tgbot/bot.sqlite3`，需要相应读权限。它核对默认、群、管理员、成员及语言菜单，清理不在命令表的旧条目，替换旧管理菜单为 `/manage`；空菜单继续继承 Telegram 的默认菜单。不会发群通知，也不是从零初始化命令菜单的工具。

更新前备份源码和所有业务数据库；只上传源文件、文档、模板与测试，不覆盖 `.env` 或数据库。更新后核对启用群数量、30 秒设置、权限及后台错误。使用模拟 API 测试，不用真实群员做处罚试验。确认服务能启动不等于所有外部 API 永久可用。

## 6. 备份与恢复

主群数据库为 `/var/lib/tgbot/bot.sqlite3`，其他群为 `/var/lib/tgbot/groups/<群ID>/bot.sqlite3`。使用 Python `sqlite3.Connection.backup()` 可生成一致性快照；备份目录仅维护人员可读。配置和凭据另外安全备份，不能上传公开仓库。

恢复时先停服，恢复匹配版本的源码、全部数据库和配置，确认数据库归属 `tgbot:tgbot`，再启动。恢复旧数据库会恢复旧处理位置和任务，可能重新处理恢复点之后的更新；检查审计与事件去重。重新开始的实例不要同时连接同一 token。

## 7. 排查

| 现象 | 先检查 |
|---|---|
| 所有群无响应 | 服务状态、token、网络、webhook 冲突和是否存在第二个轮询实例 |
| 单群无响应 | `/groups` 启用状态、机器人群权限、租户初始化日志 |
| 消息未删除 | 本群删除设置、是否仍待验证/审批/投票、删除权限和错误记录 |
| 管理按钮无效 | 所属群是否启用、面板是否超过 5 分钟、操作者是否为打开它的当前管理员 |
| 用户名无法解析 | 用户是否已收录、用户名是否变更；回复用户消息或使用数字 ID |
| 未加积分 | 每日额度、头像可见性、验证状态、消息类型及是否重复更新 |
| AI 无法解释图片 | 原图片是否仍附带、相册是否记录、格式/大小、下载或上游错误 |
| Jev 未处罚 | 必须手动 `/report`、是否达到确认条件、发送者是否豁免、接口是否失败 |
