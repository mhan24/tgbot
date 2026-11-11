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
import threading
import time

from ai import AI, AIError
from ai import NO_KEY as AI_NO_KEY, USAGE as AI_USAGE
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
        self.db = db or sqlite3.connect(cfg.get('DB_PATH') or 'bot.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS processed(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at INTEGER,actor INTEGER,
          target INTEGER,action TEXT,detail TEXT);
        ''')
        self.verification = Verification(self)
        self.exchange = Exchange(self.db)
        self.bin_lookup = BinLookup(self.db)
        self.ai = AI(api_base=cfg.get('AI_API_BASE', ''), api_key=cfg.get('AI_API_KEY', ''),
                     model=cfg.get('AI_MODEL', ''), exa_key=cfg.get('EXA_API_KEY', ''),
                     timeout=self._bounded(cfg.get('AI_TIMEOUT'), 60, 10, 180),
                     typesafe_key=cfg.get('TYPESAFE_API_KEY', ''),
                     typesafe_model=cfg.get('TYPESAFE_MODEL', ''),
                     system=cfg.get('AI_PROMPT', ''))
        self.ai_sticker = (cfg.get('AI_THINKING_STICKER') or '').strip()
        # 问答在后台线程执行，这里只保存并发与冷却状态，不访问数据库。
        self._ai_lock = threading.Lock()
        self._ai_running = 0
        self._ai_last = {}
        # 最近几条文字消息只留在内存里，供 /ai 引用目标消息与上文；不写数据库。
        self._recent_lock = threading.Lock()
        self._recent = {}
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
        if method == 'banChatMember':
            kwargs['revoke_messages'] = True
        try:
            return self.api.call(method, **kwargs)
        except APIError as exc:
            if (method == 'answerCallbackQuery' and exc.permanent and
                    any(term in exc.description.lower()
                        for term in ('query is too old', 'query id is invalid'))):
                LOG.info('Callback acknowledgement expired; business state retained')
                return False
            raise

    def send(self, chat, text, **kwargs):
        """唯一的发送入口；机器人发出的消息一律保留在聊天里。"""
        return self.call('sendMessage', chat_id=chat, text=text, parse_mode='HTML', **kwargs)

    def send_sticker(self, chat, sticker, reply_to=0):
        """贴纸同样走封装发送；和 Bot.send 一样不安排任何删除。"""
        payload = {'chat_id': chat, 'sticker': sticker}
        if reply_to:
            payload['reply_to_message_id'] = reply_to
            payload['allow_sending_without_reply'] = True
        return self.call('sendSticker', **payload)

    def download(self, file_path):
        """下载 Telegram 文件内容（/ai 图片识别用）；失败抛 APIError，由调用方降级。"""
        return self.api.download(file_path)

    def ai_thinking(self, chat, message_id):
        """在提问上标记“正在回答”：优先贴纸，未配置或失败时用表情回应。"""
        if not message_id:
            return
        if self.ai_sticker:
            try:
                self.send_sticker(chat, self.ai_sticker, message_id)
                return
            except APIError as exc:
                LOG.warning('Thinking sticker failed code=%s', exc.code)
        try:
            self.call('setMessageReaction', chat_id=chat, message_id=message_id,
                      reaction=[{'type': 'emoji', 'emoji': self.AI_THINKING_REACTION}])
        except APIError as exc:
            LOG.info('Thinking reaction failed code=%s', exc.code)

    def pending_verification(self):
        """仍有待收尾的验证时缩短长轮询，让超时、批准和恢复重试及时执行。"""
        return bool(self.db.execute(
            "SELECT 1 FROM verification WHERE state='challenge' "
            "OR (state='passed' AND ((kind='request' AND approved=0) "
            "OR (kind='member' AND restored=0))) LIMIT 1").fetchone())

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

    # --- 消息上下文 -------------------------------------------------------

    @staticmethod
    def display_name(user):
        user = user or {}
        name = str(user.get('first_name') or '').strip() or '成员'
        username = str(user.get('username') or '').strip()
        return f'{name}（@{username}）' if username else name

    def remember(self, message):
        """记住群/私聊里最近的文字消息，供 /ai 引用目标消息和上文。"""
        chat = (message.get('chat') or {}).get('id')
        sender = message.get('from') or {}
        text = (message.get('text') or '').strip()
        if (not chat or not text or text.startswith('/') or message.get('sender_chat')
                or sender.get('is_bot') or sender.get('id') == self.bot_id):
            return
        entry = {'id': int(message.get('message_id') or 0),
                 'author': self.display_name(sender),
                 'text': text[:self.AI_CONTEXT_CHARS]}
        with self._recent_lock:
            items = self._recent.setdefault(chat, [])
            if items and items[-1]['id'] == entry['id']:
                return
            items.append(entry)
            del items[:-self.AI_HISTORY * 2]

    def ai_context(self, message):
        """被回复的目标消息 + 提问前后的最近发言；没有引用时只取提问前的发言。"""
        chat = message['chat']['id']
        quoted = message.get('reply_to_message') or {}
        anchor = int(quoted.get('message_id') or message.get('message_id') or 0)
        with self._recent_lock:
            items = [item for item in self._recent.get(chat, ()) if item['id'] != anchor]
        lines = []
        quoted_text = (quoted.get('text') or quoted.get('caption') or '').strip()
        if self.ai_image_id(quoted):
            head = f'[引用] {self.display_name(quoted.get("from") or {})}：（图片）'
            lines.append(head + (' ' + quoted_text[:self.AI_CONTEXT_CHARS] if quoted_text else ''))
        elif quoted_text:
            lines.append(f'[引用] {self.display_name(quoted.get("from") or {})}：'
                         f'{quoted_text[:self.AI_CONTEXT_CHARS]}')
        lines.extend(f'[聊天] {item["author"]}：{item["text"]}' for item in items[-self.AI_HISTORY:])
        return lines

    def ai_image_id(self, quoted):
        """被引用消息里的图片 file_id：照片取最大尺寸，图片文件限 AI_IMAGE_BYTES；没有返回空。"""
        photo = quoted.get('photo')
        if photo:
            return (photo[-1] or {}).get('file_id') or ''
        doc = quoted.get('document') or {}
        try:
            size = int(doc.get('file_size') or 0)
        except (TypeError, ValueError):
            size = 0
        if str(doc.get('mime_type') or '').startswith('image/') and 0 < size <= self.AI_IMAGE_BYTES:
            return doc.get('file_id') or ''
        return ''

    def ai_image_bytes(self, file_id):
        """getFile 后下载图片字节；失败记日志并返回 None，本次回答降级为纯文字。"""
        try:
            info = self.call('getFile', file_id=file_id)
            return self.download(info.get('file_path') or '') or None
        except APIError as exc:
            LOG.warning('AI image download failed code=%s', exc.code)
            return None

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
        self.remember(message)
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
        elif name == '/ai':
            self.ai_reply(message)
        elif name:
            # 只有未知命令才回复，纯文字不回应，避免被用来刷接口配额。
            self.send(uid, '尚未提供这个命令，发送 /help 查看可用功能。')

    def group_message(self, message):
        chat_id = message['chat']['id']
        sender = message.get('from') or {}
        if message.get('sender_chat'):
            # 频道身份发言不进入业务处理。
            return
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
        if name == '/ai':
            self.ai_reply(message)
            return
        if name:
            self.send(chat_id, '尚未提供这个命令，发送 /help 查看可用功能。')

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
            self.send(chat, str(exc))
            return
        self.send(chat, text)

    def bin_reply(self, message):
        chat = message['chat']['id']
        try:
            text = self.bin_lookup.query((message.get('text') or '').split()[1:])
        except BinError as exc:
            self.send(chat, str(exc))
            return
        self.send(chat, text)

    # --- AI 问答（后台线程） ----------------------------------------------

    AI_CONCURRENCY = 2      # 同时生成中的回答
    AI_PENDING = 6          # 进行中 + 排队上限，超出直接提示稍后再试
    AI_COOLDOWN = 15        # 同一用户两次提问的最小间隔（秒）
    AI_HISTORY = 5          # /ai 引用的聊天记录条数
    AI_CONTEXT_CHARS = 500  # 单条上下文截断长度
    AI_IMAGE_BYTES = 5 * 1024 * 1024  # 被引用图片文件的大小上限，超出不识别
    AI_THINKING_REACTION = '🤔'   # 思考中的表情回应；配置了 AI_THINKING_STICKER 时改用贴纸
    AI_FAILED = '⚠️ AI 问答失败，请稍后重试。'

    def ai_reply(self, message):
        """AI 问答放到后台线程：模型与搜索都慢，不能阻塞长轮询。"""
        chat = message['chat']['id']
        uid = int((message.get('from') or {}).get('id') or 0)
        parts = (message.get('text') or '').split(maxsplit=1)
        question = parts[1].strip() if len(parts) > 1 else ''
        if not question:
            self.send(chat, AI_USAGE)
            return
        if not self.ai.enabled:
            self.send(chat, AI_NO_KEY)
            return
        now = time.time()
        with self._ai_lock:
            if len(self._ai_last) > 500:
                self._ai_last = {key: stamp for key, stamp in self._ai_last.items()
                                 if now - stamp < 3600}
            waiting = now - self._ai_last.get(uid, 0)
            if waiting < self.AI_COOLDOWN:
                refusal = f'提问太频繁，请 {int(self.AI_COOLDOWN - waiting) + 1} 秒后再试。'
            elif self._ai_running >= self.AI_CONCURRENCY + self.AI_PENDING:
                refusal = '当前提问较多，请稍后再试。'
            else:
                refusal = ''
                self._ai_last[uid] = now
                self._ai_running += 1
        if refusal:
            self.send(chat, refusal)
            return
        message_id = message.get('message_id')
        self.ai_thinking(chat, message_id)
        context = self.ai_context(message)
        try:
            threading.Thread(target=self._ai_worker,
                             args=(chat, question, message_id, context,
                                   self.ai_image_id(message.get('reply_to_message') or {})),
                             daemon=True).start()
        except RuntimeError:
            with self._ai_lock:
                self._ai_running = max(0, self._ai_running - 1)
            self._ai_send(chat, message_id, self.AI_FAILED)

    def _ai_worker(self, chat, question, message_id, context=None, image_id=''):
        """后台线程：只发起 HTTP 调用，不碰主循环使用的 SQLite 连接。"""
        try:
            try:
                image = self.ai_image_bytes(image_id) if image_id else None
                if image_id and image is None and context is not None:
                    context.append('[系统提示] 图片下载失败，本次回答看不到图片内容。')
                text = self.ai.ask(question, context=context, image=image)
            except AIError as exc:
                LOG.warning('AI answer failed code=%s', getattr(exc, 'code', 0))
                text = f'⚠️ {exc}'
            except Exception:
                LOG.exception('AI worker failed')
                text = self.AI_FAILED
            self._ai_send(chat, message_id, text)
        finally:
            with self._ai_lock:
                self._ai_running = max(0, self._ai_running - 1)

    def _ai_send(self, chat, message_id, text):
        """回答作为引用提问的回复发出；长回答拆成多条，消息一律保留。"""
        payload = {}
        if message_id:
            payload = {'reply_to_message_id': message_id, 'allow_sending_without_reply': True}
        for part in (text if isinstance(text, list) else [text]):
            try:
                self.send(chat, part, **payload)
            except APIError as exc:
                LOG.warning('AI answer delivery failed code=%s', exc.code)

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
                '/bin — 卡 BIN 查询：/bin 45717360\n'
                '/ai — AI 问答（可联网搜索）：/ai SQLite 的 WAL 模式有什么代价？')

    def start_text(self):
        return (f'👋 你好，这是为群 {html.escape(self.group_label)} 提供服务的机器人。\n'
                '入群申请会先私聊发送验证题：请先关注关联频道，再点击正确答案。\n'
                '发送 /help 查看完整说明。')

    # --- 主循环 -----------------------------------------------------------

    # 每个后台任务的最短间隔；失败后统一冷却 30 秒起。
    MAINTENANCE_TASKS = (('verification', 0), ('processed', 600))
    PROCESSED_LIMIT = 5000

    def maintenance(self):
        next_run = getattr(self, '_next_run', {})
        actions = {'verification': self.verification.tick,
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
                quick = self.pending_verification()
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
