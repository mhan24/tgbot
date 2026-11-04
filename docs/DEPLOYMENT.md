# 配置、部署与维护

核对日期：2026-11-04。生产路径由仓库 `tgbot.service` 定义；可按实际主机调整。当前版本只包含入群验证功能。

## 1. 运行条件

Python 3.9+，仅标准库。运行用户需要读配置、读写数据库，并能连接 Telegram API。

| 环境变量 | 用途 / 默认值 |
|---|---|
| `TELEGRAM_BOT_TOKEN` | 必填，机器人 token |
| `GROUP_ID` | 必填，默认群数字 ID；缺省时回退 `-1003014905211` |
| `CHANNEL_ID` | 必填，验证频道数字 ID |
| `TELEGRAM_CHANNEL` / `TELEGRAM_CHANNEL_URL` | “关注频道”按钮链接，可填 `@频道用户名` 或 t.me 链接；留空则卡片不显示该按钮 |
| `TELEGRAM_GROUP` | 仅用于 `/start` 文案里的群名说明 |
| `VERIFY_SECONDS` | 验证有效期，30–300 秒，默认 240；超出范围自动收敛 |
| `DELETE_AFTER_SECONDS` | 群内机器人消息自动清理，0–3600 秒，默认 30；0 为关闭 |
| `DB_PATH` | SQLite 路径，默认 `bot.sqlite3`；systemd 覆盖为 `/var/lib/tgbot/bot.sqlite3` |
| `SSH_HOST` / `SSH_PORT` / `SSH_USER` / `SSH_PASSWORD` | 可选本地部署连接信息，机器人运行不使用；不要复制到服务器运行配置 |

复制 `.env.example` 为 `.env` 并设置权限 `600`（服务专用用户读取时为 `640` 且属组为服务组）。环境变量优先于文件配置。不要在 Git、日志或文档中保存真实 token 与密码。

旧版 `.env` 中的 `JEV_*`、`MESSAGE_POINTS`、`MESSAGE_DAILY_LIMIT`、`UPGRADE_*`、`MUTE_SECONDS` 已不再读取，可择机清理，不清理也不影响运行。

## 2. 群与频道权限

机器人必须是默认群和验证频道的管理员：

- 频道：能读取成员状态（`getChatMember`）。
- 群：删除消息、限制成员、邀请成员（`can_delete_messages`、`can_restrict_members`、`can_invite_users`）。群主身份无需这些权限。

建议在群里开启“入群申请”，让验证在私聊完成；否则成员直接进群，由机器人先禁言再在群内验证。启动时会校验以上权限，缺少权限将直接退出并报错。

没有处理位置的首次启动会跳过积压更新。启动检测到已有 webhook 时拒绝替换，应先确认该 token 的使用方式。不要启动两个同时轮询该 token 的实例。

## 3. systemd 部署

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

源码目录需对 `tgbot` 可读。服务使用 `UMask=0077`、只读系统目录和专用 StateDirectory，数据库不能放在被只读保护的源码目录。

## 4. 验证与更新

```sh
cd /opt/tgbot
python3 -m unittest discover -s tests -q
systemctl restart tgbot
systemctl is-active tgbot
journalctl -u tgbot -n 50 --no-pager
```

启动日志应包含 `Started @用户名 group=群ID`。功能验证建议用自己的测试账号申请入群，确认：私聊收到卡片、未关注时提示先关注、答对后自动批准、答错 3 次或超时被拒绝、验证卡片在结束后按 30 秒清理。

菜单文案变化后运行 `python3 scripts/update_commands.py`（只更新已有菜单，不发送群消息），再用 Telegram 客户端确认命令菜单只剩 `/start`、`/help`。

## 5. 从旧版本升级（破坏性，无迁移）

本版本不做数据迁移：验证表结构变化后不会自动改表。升级时先停服并重建验证表：

```sh
systemctl stop tgbot
sqlite3 /var/lib/tgbot/bot.sqlite3 \
  'DROP TABLE IF EXISTS verification; DROP TABLE IF EXISTS verification_prompts;'
# 上传新代码后
systemctl start tgbot
```

- 旧验证记录、进行中的验证和旧验证卡片记录会被清空；进行中的申请人需要重新申请。
- 旧版积分、签到、举报等表不读取也不自动删除；要彻底重置需另行授权后手工 `DROP TABLE`。
- 结构不匹配时服务会启动失败并报 `verification table has an incompatible schema`，按上面步骤重建表即可。

## 6. 备份与恢复

```sh
systemctl stop tgbot
cp -a /var/lib/tgbot/bot.sqlite3* /备份路径/
systemctl start tgbot
```

或使用 SQLite backup API 在线备份。`.env` 与数据库分开存放；备份文件不提交到 Git。

## 7. 排查

| 现象 | 排查方向 |
|---|---|
| 启动报 `Bot must be administrator in channel` | 机器人未加入频道或不是频道管理员 |
| 启动报 `Missing administrator right ...` | 群内管理员权限缺少删除消息、限制成员或邀请成员 |
| 申请后收不到验证卡片 | 用户与机器人私聊被拦截（`Forbidden: bot can't initiate conversation`）；此时申请会被拒绝并记录审计，用户需先私聊机器人发 `/start` |
| 直接进群的成员无法发言 | 正在等待验证；答对后自动恢复，超时会被移出 |
| 验证卡片没有清理 | `DELETE_AFTER_SECONDS=0` 关闭了清理；恢复为非 0 后重启 |
| 日志出现 `Verification conclusion deferred` | 机器人缺少限制/邀请权限或 Telegram 暂时失败；记录保留并每 30 秒重试 |
| 日志出现 `Restore permissions failed` | 恢复发言失败，后台最多重试 5 次；仍未成功时在 Telegram 手动解除限制 |
| 启动报 `verification table has an incompatible schema` | 数据库仍是旧结构；按第 5 节删除验证相关表后重启，不做自动迁移 |

日志只记录动作、标识和错误码，不记录 token、完整请求头或消息原文。
