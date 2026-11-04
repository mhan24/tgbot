#!/usr/bin/env python3
"""Telegram 群管机器人运行核心：单群过滤、入群验证、消息生命周期与长轮询。

只依赖 Python 标准库。所有运行参数来自 `.env` / 环境变量，修改后需重启服务。
分发层只做路由，业务规则写在各功能模块里。
"""
import html
import logging
import os
from pathlib import Path
import re
import sqlite3
import time

from retention import NOTICE_SECONDS
from binlookup import BinError, BinLookup
from botapi import API, APIError
from exchange import Exchange, RateError
from verification import MAX_ATTEMPTS, Verification

LOG = logging.getLogger('groupbot')

DEFAULT_GROUP = -1003014905211
DEFAULT_VERIFY_SECONDS = 240
MIN_VERIFY_SECONDS = 30
MAX_VERIFY_SECONDS = 300
PERMISSIONS = ('can_send_messages can_send_audios can_send_documents can_send_photos '
               'can_send_videos can_send_video_notes can_send_voice_notes can_send_polls '
               'can_send_other_messages can_add_web_page_previews can_react_to_messages '
               'can_edit_tag can_change_info can_invite_users can_pin_messages '
               'can_manage_topics').split()


def config(path='.env'):
    """环境变量优先；缺少关键配置时回退读取 .env 文件。"""
    values = {}
    required = ('TELEGRAM_BOT_TOKEN', 'GROUP_ID', 'CHANNEL_ID')
    if not all(os.environ.get(key) for key in required) and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                values[key.strip()] = value.strip()
    return values | {key: value for key, value in os.environ.items() if value}


class Bot:
    def __init__(self, cfg, api=None, db=None):
        if not cfg.get('CHANNEL_ID'):
            raise RuntimeError('CHANNEL_ID is required (verification channel id)')
        if api is None and not cfg.get('TELEGRAM_BOT_TOKEN'):
            raise RuntimeError('TELEGRAM_BOT_TOKEN is required')
        self.api = api or API(cfg['TELEGRAM_BOT_TOKEN'])
        self.group = int(cfg.get('GROUP_ID') or DEFAULT_GROUP)
        self.group_label = str(cfg.get('TELEGRAM_GROUP') or self.group)
        self.channel = int(cfg['CHANNEL_ID'])
        self.channel_url = self._configured_channel_url(cfg)
        self.verify_seconds = self._bounded(cfg.get('VERIFY_SECONDS'), DEFAULT_VERIFY_SECONDS,
                                            MIN_VERIFY_SECONDS, MAX_VERIFY_SECONDS)
        # 群内机器人消息的自动清理时长；0 关闭自动清理。
        self.delete_after = self._bounded(cfg.get('DELETE_AFTER_SECONDS'), 30, 0, 3600)
        self.db = db or sqlite3.connect(cfg.get('DB_PATH') or 'bot.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS processed(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at INTEGER,actor INTEGER,
          target INTEGER,action TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS deletions(chat INTEGER,message_id INTEGER,
          attempts INTEGER DEFAULT 0,due INTEGER,PRIMARY KEY(chat,message_id));
        ''')
        self.verification = Verification(self)
        self.exchange = Exchange(self.db)
        self.bin_lookup = BinLookup(self.db, api_key=cfg.get('BIN_API_KEY', ''))
        self.username = ''
        self.bot_id = 0

    @staticmethod
    def _bounded(value, default, low, high):
        try:
            return min(high, max(low, int(value)))
        except (TypeError, ValueError):
            return default

    @staticmethod
    def _configured_channel_url(cfg):
        value = (cfg.get('TELEGRAM_CHANNEL_URL') or cfg.get('TELEGRAM_CHANNEL') or '').strip()
        if value.startswith('@'):
            return 'https://t.me/' + value[1:]
        if re.fullmatch(r'[A-Za-z0-9_]{5,}', value):
            return 'https://t.me/' + value
        if value.startswith(('https://t.me/', 'https://telegram.me/')):
            return value
        return ''

    # --- Telegram 调用与消息生命周期 --------------------------------------

    def call(self, method, **kwargs):
        keep = kwargs.pop('_keep', False)
        delete_after = kwargs.pop('_delete_after', None)
        if method == 'banChatMember':
            kwargs['revoke_messages'] = True
        try:
            result = self.api.call(method, **kwargs)
        except APIError as exc:
            if (method == 'answerCallbackQuery' and exc.permanent and
                    any(term in exc.description.lower()
                        for term in ('query is too old', 'query id is invalid'))):
                LOG.info('Callback acknowledgement expired; business state retained')
                return False
            raise
        if method == 'sendMessage' and isinstance(result, dict):
            if delete_after is not None or not keep:
                self.schedule_delete(kwargs.get('chat_id'), result.get('message_id'), seconds=delete_after)
        return result

    def send(self, chat, text, keep=False, **kwargs):
        return self.call('sendMessage', chat_id=chat, text=text, parse_mode='HTML', _keep=keep, **kwargs)

    def notice_seconds(self):
        """失败与处罚通知的显式保留时长；群清理关闭时跟随为 0。"""
        return NOTICE_SECONDS if self.delete_after > 0 else 0

    def schedule_delete(self, chat, message_id, seconds=None):
        delay = self.delete_after if seconds is None else seconds
        if delay <= 0 or chat is None or not message_id or chat != self.group:
            return
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO deletions(chat,message_id,attempts,due) VALUES(?,?,0,?)',
                            (chat, int(message_id), int(time.time()) + delay))

    def sweep_deletions(self):
        self.queue_finished_cards()
        rows = self.db.execute('SELECT * FROM deletions WHERE due<=? ORDER BY due LIMIT 50',
                               (int(time.time()),)).fetchall()
        for row in rows:
            try:
                self.call('deleteMessage', chat_id=row['chat'], message_id=row['message_id'])
            except APIError as exc:
                if exc.permanent:
                    # 已删除或无权删除：不再重试。
                    self.drop_deletion(row)
                elif exc.code == 429:
                    self.defer_deletion(row, exc.retry_after)
                else:
                    LOG.warning('Delete failed %s:%s code=%s', row['chat'], row['message_id'], exc.code)
                    self.defer_deletion(row, 30)
                continue
            self.drop_deletion(row)

    def drop_deletion(self, row):
        with self.db:
            self.db.execute('DELETE FROM deletions WHERE chat=? AND message_id=?',
                            (row['chat'], row['message_id']))

    def defer_deletion(self, row, delay):
        if row['attempts'] + 1 >= 3:
            # 无法删除的消息不再无限重试，只记录放弃原因。
            self.drop_deletion(row)
            self.audit(0, 0, 'delete_giveup', f'{row["chat"]}:{row["message_id"]}')
            return
        with self.db:
            self.db.execute('UPDATE deletions SET attempts=attempts+1,due=? WHERE chat=? AND message_id=?',
                            (int(time.time()) + delay, row['chat'], row['message_id']))

    def queue_finished_cards(self):
        """验证结束后把群内验证卡片转入普通清理队列；关闭清理时只移除记录。"""
        rows = self.db.execute('''SELECT p.* FROM verification_prompts p
            LEFT JOIN verification v ON v.uid=p.uid
            WHERE v.uid IS NULL OR v.nonce!=p.nonce OR v.state='passed' OR p.expires<=?''',
            (int(time.time()),)).fetchall()
        for row in rows:
            with self.db:
                self.db.execute('DELETE FROM verification_prompts WHERE chat=? AND message_id=?',
                                (row['chat'], row['message_id']))
            self.schedule_delete(row['chat'], row['message_id'])

    def pending_deletions(self):
        return bool(self.db.execute('SELECT 1 FROM deletions LIMIT 1').fetchone() or
                    (self.delete_after > 0 and
                     self.db.execute('SELECT 1 FROM verification_prompts LIMIT 1').fetchone()))

    # --- 权限与成员状态 ---------------------------------------------------

    def audit(self, actor, target, action, detail=''):
        with self.db:
            self.db.execute('INSERT INTO audit(at,actor,target,action,detail) VALUES(?,?,?,?,?)',
                            (int(time.time()), actor, target, action, detail))

    def admin(self, uid):
        return self.call('getChatMember', chat_id=self.group,
                         user_id=uid)['status'] in ('creator', 'administrator')

    def subscribed(self, uid):
        member = self.call('getChatMember', chat_id=self.channel, user_id=uid)
        return member['status'] in ('creator', 'administrator', 'member') or (
            member['status'] == 'restricted' and member.get('is_member', False))

    def restrict(self, uid, until=0):
        self.call('restrictChatMember', chat_id=self.group, user_id=uid,
                  permissions={key: False for key in PERMISSIONS},
                  use_independent_chat_permissions=True, until_date=until)

    def restore(self, uid):
        """恢复成员发言权限：按本群默认权限设置。"""
        permissions = self.call('getChat', chat_id=self.group).get('permissions') or {}
        self.call('restrictChatMember', chat_id=self.group, user_id=uid,
                  permissions={key: bool(permissions.get(key, False)) for key in PERMISSIONS},
                  use_independent_chat_permissions=True)

    @staticmethod
    def active(member):
        member = member or {}
        status = member.get('status')
        return status in ('creator', 'administrator', 'member') or (
            status == 'restricted' and bool(member.get('is_member')))

    # --- 更新分发 ---------------------------------------------------------

    def accepts_update(self, update):
        payload = (update.get('message') or update.get('chat_member') or
                   update.get('chat_join_request'))
        if payload:
            chat = payload.get('chat') or {}
            return chat.get('type') == 'private' or chat.get('id') == self.group
        query = update.get('callback_query')
        if query:
            chat = (query.get('message') or {}).get('chat') or {}
            return chat.get('id') == self.group or (
                chat.get('type') == 'private' and chat.get('id') == (query.get('from') or {}).get('id'))
        return False

    def handle(self, update):
        if not self.accepts_update(update):
            return
        if 'callback_query' in update:
            self.verification.callback(update['callback_query'])
            return
        if 'chat_join_request' in update:
            request = update['chat_join_request']
            if (request.get('chat') or {}).get('id') == self.group:
                user = request.get('from') or {}
                self.verification.admit(user, 'request',
                                        request.get('user_chat_id') or user.get('id'))
            return
        if 'chat_member' in update:
            self.member_update(update['chat_member'])
            return
        message = update.get('message')
        if not message:
            return
        if (message.get('chat') or {}).get('type') == 'private':
            self.private_message(message)
        else:
            self.group_message(message)

    def member_update(self, event):
        if (event.get('chat') or {}).get('id') != self.group:
            return
        old = event.get('old_chat_member') or {}
        new = event.get('new_chat_member') or {}
        user = new.get('user') or {}
        uid = int(user.get('id') or 0)
        if not uid:
            return
        if not self.active(old) and self.active(new):
            self.verification.admit(user, 'member', self.group, event.get('from'))
        elif self.active(old) and not self.active(new):
            self.verification.forget(uid)

    def private_message(self, message):
        uid = message['chat']['id']
        if self.foreign_command(message):
            # 指向其他机器人的命令不属于本机器人，保持静默。
            return
        name = self.command_name(message)
        if name == '/start':
            self.send(uid, self.start_text())
        elif name == '/help':
            self.send(uid, self.help_text())
        elif name == '/rate':
            self.rate_reply(message)
        elif name == '/bin':
            self.bin_reply(message)
        elif name:
            # 只有未知命令才回复，纯文字不回应，避免被用来刷接口配额。
            self.send(uid, '尚未提供这个命令，发送 /help 查看可用功能。')

    def group_message(self, message):
        chat_id = message['chat']['id']
        sender = message.get('from') or {}
        if message.get('sender_chat'):
            # 频道身份发言不进入业务处理。
            return
        if sender.get('is_bot') and sender.get('id') != self.bot_id and message.get('message_id'):
            # 只有实际送达本机器人的其他 Bot 消息才能清理。
            self.schedule_delete(chat_id, message['message_id'])
        for user in message.get('new_chat_members') or []:
            self.verification.admit(user, 'member', self.group, sender)
        if not sender or sender.get('id') == self.bot_id:
            return
        name = self.command_name(message)
        if name == '/help':
            self.send(chat_id, self.help_text())
            return
        if name == '/start':
            self.send(chat_id, '请私聊机器人发送 /start。')
            return
        if self.verification.pending(sender['id']):
            # 未完成验证的成员在群内的消息不参与业务。
            return
        if name == '/rate':
            self.rate_reply(message)
            return
        if name == '/bin':
            self.bin_reply(message)
            return
        if name:
            self.send(chat_id, '尚未提供这个命令，发送 /help 查看可用功能。',
                      _delete_after=self.notice_seconds())

    def foreign_command(self, message):
        """``/cmd@其他机器人`` 不是发给本机器人的命令。"""
        text = (message.get('text') or '').strip()
        if not text.startswith('/'):
            return False
        name = text.split()[0].split('@', 1)
        return len(name) > 1 and bool(self.username) and name[1].lower() != self.username.lower()

    def command_name(self, message):
        text = (message.get('text') or '').strip()
        if not text.startswith('/'):
            return ''
        name = text.split()[0].split('@', 1)
        if len(name) > 1 and self.username and name[1].lower() != self.username.lower():
            return ''
        return name[0].lower()

    # --- 工具查询 ---------------------------------------------------------

    def rate_reply(self, message):
        chat = message['chat']['id']
        try:
            text = self.exchange.convert((message.get('text') or '').split()[1:])
        except RateError as exc:
            self.send(chat, str(exc), _delete_after=self.notice_seconds())
            return
        self.send(chat, text)

    def bin_reply(self, message):
        chat = message['chat']['id']
        try:
            text = self.bin_lookup.query((message.get('text') or '').split()[1:])
        except BinError as exc:
            self.send(chat, str(exc), _delete_after=self.notice_seconds())
            return
        self.send(chat, text)

    # --- 说明文案 ---------------------------------------------------------

    def help_text(self):
        channel = (f'\n1. 关注关联频道：{html.escape(self.channel_url)}\n2. 点击正确答案完成算术题'
                   if self.channel_url else '\n1. 关注本群关联频道\n2. 点击正确答案完成算术题')
        return ('🤖 <b>机器人说明</b>\n\n'
                '<b>入群验证</b>\n新成员入群前需完成两步验证：' + channel + '\n'
                f'验证限时 {self.verification.limit_text()}，最多答错 {MAX_ATTEMPTS} 次；'
                '超时或连续答错会被拒绝或移出，可重新申请入群。\n'
                '管理员与群主直接放行。\n\n'
                '<b>命令</b>\n'
                '/start — 私聊开始使用\n'
                '/help — 查看本说明\n'
                '/rate — 汇率换算：/rate 100 USD CNY\n'
                '/bin — 卡 BIN 查询：/bin 45717360')

    def start_text(self):
        return (f'👋 你好，这是为群 {html.escape(self.group_label)} 提供服务的机器人。\n'
                '入群申请会先私聊发送验证题：请先关注关联频道，再点击正确答案。\n'
                '发送 /help 查看完整说明。')

    # --- 主循环 -----------------------------------------------------------

    # 每个后台任务的最短间隔；失败后统一冷却 30 秒起。
    MAINTENANCE_TASKS = (('verification', 0), ('deletions', 0), ('processed', 600))
    PROCESSED_LIMIT = 5000

    def maintenance(self):
        next_run = getattr(self, '_next_run', {})
        actions = {'verification': self.verification.tick,
                   'deletions': self.sweep_deletions,
                   'processed': self.prune_processed}
        now = time.time()
        for name, interval in self.MAINTENANCE_TASKS:
            if next_run.get(name, 0) > now:
                continue
            try:
                actions[name]()
            except Exception as exc:
                next_run[name] = time.time() + max(30, getattr(exc, 'retry_after', 0))
                LOG.warning('Maintenance deferred task=%s type=%s', name, type(exc).__name__)
            else:
                next_run[name] = time.time() + interval
        self._next_run = next_run

    def prune_processed(self):
        """只保留最近的更新 ID：处理位置已经前移，旧 ID 不会被重新投递。"""
        with self.db:
            self.db.execute('DELETE FROM processed WHERE id <= (SELECT max(id) - ? FROM processed)',
                            (self.PROCESSED_LIMIT,))

    def validate_permissions(self):
        channel_member = self.call('getChatMember', chat_id=self.channel, user_id=self.bot_id)
        if channel_member.get('status') not in ('administrator', 'creator'):
            raise RuntimeError(f'Bot must be administrator in channel {self.channel}')
        member = self.call('getChatMember', chat_id=self.group, user_id=self.bot_id)
        if member.get('status') not in ('administrator', 'creator'):
            raise RuntimeError(f'Bot must be administrator in group {self.group}')
        if member.get('status') != 'creator':
            for right in ('can_delete_messages', 'can_restrict_members', 'can_invite_users'):
                if not member.get(right):
                    raise RuntimeError(f'Missing administrator right {right} in group {self.group}')

    def run(self):
        me = self.call('getMe')
        self.username, self.bot_id = me['username'], me['id']
        if self.call('getWebhookInfo').get('url'):
            raise RuntimeError('Existing webhook configured; refusing to replace it')
        self.validate_permissions()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('start_time',?)", (str(int(time.time())),))
        start_time = int(self.db.execute("SELECT value FROM meta WHERE key='start_time'").fetchone()[0])
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        offset = int(row[0]) if row else 0
        LOG.info('Started @%s group=%s', self.username, self.group)
        while True:
            try:
                self.maintenance()
                quick = self.pending_deletions()
                updates = self.call('getUpdates', offset=offset, timeout=1 if quick else 20,
                                    allowed_updates=['message', 'chat_member', 'chat_join_request',
                                                     'callback_query'])
                for update in updates:
                    payload = next((update[key] for key in ('message', 'chat_member', 'chat_join_request')
                                    if key in update), {})
                    event_time = payload.get('date', start_time)
                    if (self.accepts_update(update) and event_time >= start_time and
                            not self.db.execute('SELECT 1 FROM processed WHERE id=?',
                                                (update['update_id'],)).fetchone()):
                        try:
                            self.handle(update)
                        except APIError as exc:
                            if not exc.permanent:
                                raise
                            # 过期申请、失效回调不能让后续更新全部卡住。
                            LOG.error('Permanent failure update=%s code=%s', update['update_id'], exc.code)
                            self.audit(0, 0, 'update_failed', f'{update["update_id"]}: {exc.code}')
                    offset = update['update_id'] + 1
                    with self.db:
                        self.db.execute('INSERT OR IGNORE INTO processed VALUES(?)', (update['update_id'],))
                        self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))
            except APIError as exc:
                if exc.code == 409:
                    # 同一个 token 被两个进程轮询时 Telegram 返回 409。
                    LOG.error('Polling conflict: another instance is polling this token')
                else:
                    LOG.warning('Polling failed code=%s retry_after=%s', exc.code, exc.retry_after)
                time.sleep(min(max(exc.retry_after, 3), 60))
            except Exception:
                LOG.exception('Update failed; will retry')
                time.sleep(5)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    Bot(config()).run()
