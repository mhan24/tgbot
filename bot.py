#!/usr/bin/env python3
"""Telegram 群管：多群分发、验证、管理服务与持久化消息清理。仅使用标准库。"""
from appeals import Appeals
from milestones import Milestones
import html
import json
import logging
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

from binlookup import BinLookup, LookupError, format_result
from points import Points
from airdrop import Airdrops
from ai_chat import AIChat
from attention import Attention
from history import History
from members import Members
from jev_ads import JevAds
from moderation import Moderation

LOG = logging.getLogger('groupbot')
# Chinese triggers remain usable for existing members, while Telegram's
# command menu and /help keep the English canonical command names only.
POINTS_COMMAND_ALIASES = {
    '签到': '/checkin',
    '积分': '/points',
    '排行榜': '/rank',
    '原神榜': '/milestones',
}
PERMISSIONS = ('can_send_messages can_send_audios can_send_documents can_send_photos '
               'can_send_videos can_send_video_notes can_send_voice_notes can_send_polls '
               'can_send_other_messages can_add_web_page_previews can_react_to_messages '
               'can_edit_tag can_change_info can_invite_users can_pin_messages can_manage_topics').split()


def config(path='.env'):
    values = {}
    if not all(os.environ.get(k) for k in ('TELEGRAM_BOT_TOKEN', 'GROUP_ID', 'CHANNEL_ID')) and Path(path).exists():
        for line in Path(path).read_text().splitlines():
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                values[k] = v
    return values | dict(os.environ)


class APIError(Exception):
    def __init__(self, method, data):
        self.code = data.get('error_code', 0)
        self.description = data.get('description', 'Network failure')
        self.retry_after = data.get('parameters', {}).get('retry_after', 5)
        super().__init__(f'{method}: {self.code} {self.description}')


class API:
    def __init__(self, token):
        self.base = 'https://api.telegram.org/bot' + token + '/'

    def call(self, method, **data):
        request = urllib.request.Request(self.base + method, data=json.dumps(data).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                result = json.load(exc)
            except Exception:
                result = {'error_code': exc.code, 'description': 'HTTP error'}
        except (OSError, urllib.error.URLError):
            raise APIError(method, {'description': 'Network failure'}) from None
        if not result.get('ok'):
            raise APIError(method, result)
        return result['result']




class Bot:
    GROUP_SETTING_SPECS = {
        'CHECKIN_MIN': {'type': 'int', 'min': 1, 'max': 1000, 'label': '签到积分下限'},
        'CHECKIN_MAX': {'type': 'int', 'min': 1, 'max': 1000, 'label': '签到积分上限'},
        'MESSAGE_POINTS': {'type': 'int', 'min': 1, 'max': 100, 'label': '每条发言积分'},
        'MESSAGE_DAILY_LIMIT': {'type': 'int', 'min': 0, 'max': 1000, 'label': '每日发言积分上限'},
        'MUTE_SECONDS': {'type': 'int', 'min': 60, 'max': 2592000, 'label': '管理员默认禁言秒数'},
        'VERIFY_SECONDS': {'type': 'int', 'min': 30, 'max': 300, 'label': '入群验证秒数'},
        'DELETE_AFTER_SECONDS': {'type': 'int', 'min': 0, 'max': 3600, 'label': '群内机器人消息自动删除秒数'},
        'HISTORY_MESSAGES': {'type': 'int', 'min': 0, 'max': 500, 'label': 'AI 历史记录保留条数'},
        'HISTORY_CONTEXT': {'type': 'int', 'min': 0, 'max': 200, 'label': '每次 AI 引用历史条数'},
        'JEV_ENABLED': {'type': 'bool', 'label': 'Jev 手动广告举报'},
        'UPGRADE_POINTS': {'type': 'int', 'min': 1, 'max': 1000000, 'label': '成员标签/达标积分门槛'},
        'UPGRADE_TAG': {'type': 'tag', 'label': '达标后设置的成员标签'},
        'AI_COOLDOWN_SECONDS': {'type': 'int', 'min': 0, 'max': 86400, 'label': '普通成员 AI 冷却秒数'},
    }

    def __init__(self, cfg, api=None, db=None):
        self.base_cfg = dict(cfg)
        self.cfg = dict(cfg)
        self.api = api or API(cfg['TELEGRAM_BOT_TOKEN'])
        self.group = int(cfg['GROUP_ID'])
        self.channel = int(cfg['CHANNEL_ID'])
        self.channel_url = self._configured_channel_url(cfg)
        self.mute_seconds = int(cfg.get('MUTE_SECONDS', 86400))
        self.verify_seconds = int(cfg.get('VERIFY_SECONDS', 240))
        self.upgrade_points = int(cfg.get('UPGRADE_POINTS', 50))
        self.upgrade_tag = cfg.get('UPGRADE_TAG', '家人')
        # Seconds before the bot removes its own group messages and other bots' group messages.
        self.delete_after = int(cfg.get('DELETE_AFTER_SECONDS', 30))
        # Group operations a regular member may run per day before being sent to private chat.
        self.checkin_min = int(cfg.get('CHECKIN_MIN', 1))
        self.checkin_max = int(cfg.get('CHECKIN_MAX', 5))
        self.message_points = int(cfg.get('MESSAGE_POINTS', 1))
        self.message_daily_limit = int(cfg.get('MESSAGE_DAILY_LIMIT', 5))
        self.ai_cooldown_seconds = int(cfg.get('AI_COOLDOWN_SECONDS', 60))
        self.setting_defaults = {
            'CHECKIN_MIN': self.checkin_min,
            'CHECKIN_MAX': self.checkin_max,
            'MESSAGE_POINTS': self.message_points,
            'MESSAGE_DAILY_LIMIT': self.message_daily_limit,
            'MUTE_SECONDS': self.mute_seconds,
            'VERIFY_SECONDS': self.verify_seconds,
            'DELETE_AFTER_SECONDS': self.delete_after,
            'HISTORY_MESSAGES': int(cfg.get('HISTORY_MESSAGES', 50)),
            'HISTORY_CONTEXT': int(cfg.get('HISTORY_CONTEXT', 50)),
            'JEV_ENABLED': str(cfg.get('JEV_ENABLED', '1')).lower() in ('1', 'true', 'yes', 'on'),
            'UPGRADE_POINTS': self.upgrade_points,
            'UPGRADE_TAG': self.upgrade_tag,
            'AI_COOLDOWN_SECONDS': self.ai_cooldown_seconds,
        }
        self.setting_defaults = {key: self.parse_group_setting(key, value)
                                 for key, value in self.setting_defaults.items()}
        if self.setting_defaults['CHECKIN_MIN'] > self.setting_defaults['CHECKIN_MAX']:
            raise ValueError('CHECKIN_MIN must not exceed CHECKIN_MAX')
        self.setting_values = dict(self.setting_defaults)
        self.setting_overrides = set()
        # Granted rights are unknown until run() reads them from the group membership.
        self.can_manage_tags = False
        self.db = db or sqlite3.connect(cfg.get('DB_PATH', 'bot.sqlite3'))
        self.db.row_factory = sqlite3.Row
        self.db.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS verification(uid INTEGER PRIMARY KEY, chat INTEGER, kind TEXT,
          nonce TEXT, answer INTEGER, expires INTEGER, attempts INTEGER DEFAULT 0, passed INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS warnings(uid INTEGER PRIMARY KEY,count INTEGER DEFAULT 0,mute_until INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS violations(event TEXT PRIMARY KEY,uid INTEGER,count INTEGER,reason TEXT,done INTEGER DEFAULT 0,punished INTEGER DEFAULT 0);
        CREATE TABLE IF NOT EXISTS processed(id INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY,at INTEGER,actor INTEGER,target INTEGER,action TEXT,detail TEXT);
        CREATE TABLE IF NOT EXISTS blacklist(uid INTEGER PRIMARY KEY,at INTEGER,actor INTEGER,reason TEXT);
        CREATE TABLE IF NOT EXISTS upgrades(uid INTEGER PRIMARY KEY,tag TEXT,at INTEGER,total INTEGER);
        CREATE TABLE IF NOT EXISTS deletions(chat INTEGER,message_id INTEGER,attempts INTEGER DEFAULT 0,due INTEGER,PRIMARY KEY(chat,message_id));
        CREATE TABLE IF NOT EXISTS verification_prompts(chat INTEGER,message_id INTEGER,uid INTEGER,nonce TEXT,expires INTEGER,PRIMARY KEY(chat,message_id));
        CREATE TABLE IF NOT EXISTS whitelist(uid INTEGER PRIMARY KEY,at INTEGER,actor INTEGER,note TEXT);
        CREATE TABLE IF NOT EXISTS user_activity(uid INTEGER PRIMARY KEY, last_active INTEGER);
        CREATE TABLE IF NOT EXISTS group_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        ''')
        if 'punished' not in {row[1] for row in self.db.execute('PRAGMA table_info(violations)')}:
            self.db.execute('ALTER TABLE violations ADD COLUMN punished INTEGER DEFAULT 0')
            self.db.commit()
        with self.db:
            self.db.execute('DROP TABLE IF EXISTS command_usage')
            self.db.execute("DELETE FROM group_settings WHERE key='GROUP_COMMAND_LIMIT'")
        for setting in self.db.execute('SELECT key,value FROM group_settings').fetchall():
            if setting['key'] not in self.GROUP_SETTING_SPECS:
                continue
            try:
                self.setting_values[setting['key']] = self.parse_group_setting(setting['key'], setting['value'])
                self.setting_overrides.add(setting['key'])
            except ValueError:
                LOG.warning('Ignoring invalid stored group setting key=%s group=%s', setting['key'], self.group)
        if self.setting_values['CHECKIN_MIN'] > self.setting_values['CHECKIN_MAX']:
            LOG.warning('Ignoring invalid check-in range group=%s', self.group)
            for key in ('CHECKIN_MIN', 'CHECKIN_MAX'):
                self.setting_values[key] = self.setting_defaults[key]
                self.setting_overrides.discard(key)
        self._apply_group_settings()
        self.bin_lookup = BinLookup(self.db)
        self.points = Points(self.db, self.checkin_min, self.checkin_max,
                             self.message_points, self.message_daily_limit)
        self.airdrops = Airdrops(self)
        self.ai = AIChat(self)
        self.attention = Attention(self)
        self.history = History(self, keep_messages=self.setting_values['HISTORY_MESSAGES'],
                               context_messages=self.setting_values['HISTORY_CONTEXT'])
        self.members = Members(self)
        self.jev = JevAds(self)
        self.moderation = Moderation(self)
        self.appeals = Appeals(self)
        self.milestones = Milestones(self)
        self.group_manager_root = False
        self.managed_bots = {self.group: self}
        self.manager_root = self
        self.username = ''
        self.bot_id = 0

    @classmethod
    def parse_group_setting(cls, key, value):
        spec = cls.GROUP_SETTING_SPECS[key]
        raw = str(value).strip()
        if spec['type'] == 'int':
            if not re.fullmatch(r'\d+', raw):
                raise ValueError(f'{key} 必须是整数。')
            number = int(raw)
            if not spec['min'] <= number <= spec['max']:
                raise ValueError(f'{key} 范围为 {spec["min"]}–{spec["max"]}。')
            return number
        if spec['type'] == 'bool':
            values = {'1': True, 'true': True, 'yes': True, 'on': True, '开启': True,
                      '0': False, 'false': False, 'no': False, 'off': False, '关闭': False}
            if raw.lower() not in values:
                raise ValueError(f'{key} 请输入 on/off 或 1/0。')
            return values[raw.lower()]
        if spec['type'] == 'tag':
            units = len(raw.encode('utf-16-le')) // 2
            if (not 1 <= units <= 16 or
                    any(unicodedata.category(char) in ('So', 'Cc', 'Cf') for char in raw)):
                raise ValueError('UPGRADE_TAG 需为 1–16 个 UTF-16 字符，且不能包含 Emoji。')
            return raw
        raise ValueError(f'不支持的设置项：{key}')

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

    @staticmethod
    def _channel_url_from_chat(chat, fallback=''):
        username = chat.get('username')
        if username:
            return 'https://t.me/' + username.lstrip('@')
        invite_link = chat.get('invite_link')
        if invite_link and invite_link.startswith(('https://t.me/', 'https://telegram.me/')):
            return invite_link
        return fallback

    def _apply_group_settings(self):
        """Apply this group's persisted settings to runtime fields and feature modules."""
        values = self.setting_values
        for key, value in values.items():
            self.cfg[key] = ('1' if value else '0') if isinstance(value, bool) else str(value)
        self.checkin_min = values['CHECKIN_MIN']
        self.checkin_max = values['CHECKIN_MAX']
        self.message_points = values['MESSAGE_POINTS']
        self.message_daily_limit = values['MESSAGE_DAILY_LIMIT']
        self.mute_seconds = values['MUTE_SECONDS']
        self.verify_seconds = values['VERIFY_SECONDS']
        self.delete_after = values['DELETE_AFTER_SECONDS']
        self.upgrade_points = values['UPGRADE_POINTS']
        self.upgrade_tag = values['UPGRADE_TAG']
        self.ai_cooldown_seconds = values['AI_COOLDOWN_SECONDS']
        if hasattr(self, 'points'):
            self.points.checkin_min = self.checkin_min
            self.points.checkin_max = self.checkin_max
            self.points.message_points = self.message_points
            self.points.message_daily_limit = self.message_daily_limit
        if hasattr(self, 'history'):
            self.history.keep_messages = values['HISTORY_MESSAGES']
            self.history.context_messages = values['HISTORY_CONTEXT']

    def group_setting_value(self, key):
        value = self.setting_values[key]
        if isinstance(value, bool):
            return '开启' if value else '关闭'
        return str(value) if value != '' else '未设置'

    def call(self, method, **kwargs):
        keep = kwargs.pop('_keep', False)
        ai_history = kwargs.pop('_ai_history', False)
        if method == 'banChatMember':
            kwargs['revoke_messages'] = True
        result = self.api.call(method, **kwargs)
        if method == 'getChatMember' and isinstance(result, dict):
            self.members.remember(result.get('user'))
        if method == 'banChatMember' and result:
            with self.db:
                self.db.execute('DELETE FROM chat_history WHERE chat=? AND uid=?',
                                (kwargs['chat_id'], kwargs['user_id']))
        if method == 'sendMessage' and isinstance(result, dict):
            # Every message this bot posts to the group is queued for removal.
            if not keep:
                self.schedule_delete(kwargs.get('chat_id'), result.get('message_id'))
            # Keep our own group messages in the AI's view of the conversation.
            self.history.record_outgoing(kwargs.get('chat_id'), result.get('message_id'), kwargs.get('text'),
                                         reply_to_id=(kwargs.get('reply_parameters') or {}).get('message_id'),
                                         topic_id=kwargs.get('message_thread_id') or result.get('message_thread_id'), ai=ai_history)
        return result

    def is_bot_operation(self, msg):
        """Text that triggers a bot operation in the group."""
        text = (msg.get('text') or msg.get('caption') or '').strip()
        if not text:
            return False
        first = text.split()[0]
        name = first.split('@', 1)
        if len(name) > 1 and name[1].lower() != self.username.lower():
            return False
        cmd = POINTS_COMMAND_ALIASES.get(name[0].lower(), name[0].lower())
        return cmd in ('/help', '/checkin', '/points', '/rank', '/milestones', '/bin', '/ai')

    def schedule_delete(self, chat, message_id):
        if self.delete_after <= 0 or chat is None or not message_id or chat != self.group:
            return
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO deletions(chat,message_id,attempts,due) VALUES(?,?,0,?)',
                            (chat, int(message_id), int(time.time()) + self.delete_after))

    def drop_deletion(self, row):
        with self.db:
            self.db.execute('DELETE FROM deletions WHERE chat=? AND message_id=?', (row['chat'], row['message_id']))

    def defer_deletion(self, row, delay):
        if row['attempts'] + 1 >= 3:
            # Give up instead of retrying forever on a message we cannot remove.
            self.drop_deletion(row)
            self.audit(0, 0, 'delete_giveup', f"{row['chat']}:{row['message_id']}")
            return
        with self.db:
            self.db.execute('UPDATE deletions SET attempts=attempts+1,due=? WHERE chat=? AND message_id=?',
                            (int(time.time()) + delay, row['chat'], row['message_id']))

    def sweep_deletions(self):
        self.queue_finished_verifications()
        rows = self.db.execute('SELECT * FROM deletions WHERE due<=? ORDER BY due LIMIT 50',
                               (int(time.time()),)).fetchall()
        for row in rows:
            try:
                self.call('deleteMessage', chat_id=row['chat'], message_id=row['message_id'])
            except APIError as exc:
                if exc.code in (400, 403):
                    # Already gone, or not deletable: stop retrying.
                    self.drop_deletion(row)
                elif exc.code == 429:
                    self.defer_deletion(row, max(exc.retry_after, 5))
                else:
                    LOG.warning('Delete failed %s:%s %s', row['chat'], row['message_id'], exc)
                    self.defer_deletion(row, 30)
                continue
            self.drop_deletion(row)

    def pending_deletions(self):
        return bool(self.db.execute('SELECT 1 FROM deletions LIMIT 1').fetchone() or
                    (self.delete_after > 0 and self.db.execute('SELECT 1 FROM verification_prompts LIMIT 1').fetchone()))

    def queue_finished_verifications(self):
        if self.delete_after <= 0:
            return
        rows = self.db.execute('''SELECT p.* FROM verification_prompts p
            LEFT JOIN verification v ON v.uid=p.uid
            WHERE v.uid IS NULL OR v.nonce!=p.nonce OR v.kind='approved' OR p.expires<=?''',
            (int(time.time()),)).fetchall()
        for row in rows:
            with self.db:
                self.schedule_delete(row['chat'], row['message_id'])
                self.db.execute('DELETE FROM verification_prompts WHERE chat=? AND message_id=?',
                                (row['chat'], row['message_id']))

    def send(self, chat, text, keep=False, **kwargs):
        # keep=True exempts interactive messages whose buttons must outlive the auto-delete timer.
        return self.call('sendMessage', chat_id=chat, text=text, parse_mode='HTML', _keep=keep, **kwargs)

    def audit(self, actor, target, action, detail=''):
        with self.db:
            self.db.execute('INSERT INTO audit(at,actor,target,action,detail) VALUES(?,?,?,?,?)',
                            (int(time.time()), actor, target, action, detail))

    def admin(self, uid):
        return self.call('getChatMember', chat_id=self.group, user_id=uid)['status'] in ('creator', 'administrator')

    def blacklisted(self, uid):
        return bool(self.db.execute('SELECT 1 FROM blacklist WHERE uid=?', (uid,)).fetchone())

    def whitelisted(self, uid):
        """Trusted members skip Jev review, verification and three-day auto-blacklisting.
        A blacklist entry always wins, so whitelisting can never undo a ban."""
        if not uid or self.blacklisted(uid):
            return False
        return bool(self.db.execute('SELECT 1 FROM whitelist WHERE uid=?', (uid,)).fetchone())

    def exempt(self, uid):
        """Administrators and whitelisted members bypass the routine restrictions."""
        return bool(uid) and (self.whitelisted(uid) or self.admin(uid))

    def upgrade_member(self, uid):
        """Tag a member once they pass the points threshold; an existing tag is never overwritten."""
        self.milestones.achieve(uid)
        if uid <= 0 or uid == self.bot_id or not self.can_manage_tags:
            return
        row = self.db.execute('SELECT total FROM point_users WHERE uid=?', (uid,)).fetchone()
        if not row or row[0] < self.upgrade_points:
            return
        if self.db.execute('SELECT 1 FROM upgrades WHERE uid=?', (uid,)).fetchone():
            return
        try:
            member = self.call('getChatMember', chat_id=self.group, user_id=uid)
        except APIError:
            return
        status = member.get('status')
        regular = status == 'member' or (status == 'restricted' and member.get('is_member'))
        if not regular:
            # Owners and administrators use custom titles, and non-members cannot be tagged.
            self.record_upgrade(uid, 'skipped', row[0])
            return
        if member.get('tag'):
            # The member already set their own tag; leave it alone.
            self.record_upgrade(uid, 'custom', row[0])
            return
        try:
            self.call('setChatMemberTag', chat_id=self.group, user_id=uid, tag=self.upgrade_tag)
        except APIError as exc:
            LOG.warning('Tag upgrade failed uid=%s: %s', uid, exc)
            return
        self.record_upgrade(uid, self.upgrade_tag, row[0])
        self.audit(0, uid, 'upgrade_tag', f'{self.upgrade_tag} @ {row[0]} 分')

    def record_upgrade(self, uid, tag, total):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO upgrades VALUES(?,?,?,?)',
                            (uid, tag, int(time.time()), total))

    def blacklist_member(self, uid, actor=0, reason=''):
        """Remove a member for good: ban prevents re-joining, the row keeps them out on rejoin."""
        if uid <= 0 or uid == self.bot_id:
            return False
        with self.db:
            self.db.execute('INSERT INTO blacklist(uid,at,actor,reason) VALUES(?,?,?,?) '
                            'ON CONFLICT(uid) DO UPDATE SET at=excluded.at,actor=excluded.actor,reason=excluded.reason',
                            (uid, int(time.time()), actor, reason[:200]))
            self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
            self.db.execute('DELETE FROM whitelist WHERE uid=?', (uid,))
            self.db.execute('UPDATE warnings SET mute_until=0 WHERE uid=?', (uid,))
        self.call('banChatMember', chat_id=self.group, user_id=uid, revoke_messages=True)
        self.audit(actor, uid, 'blacklist', reason)
        return True

    def unblacklist_member(self, uid, actor=0):
        """Clear local punishment only after Telegram successfully lifts the ban."""
        self.call('unbanChatMember', chat_id=self.group, user_id=uid, only_if_banned=True)
        with self.db:
            self.db.execute('DELETE FROM blacklist WHERE uid=?', (uid,))
            self.db.execute('DELETE FROM whitelist WHERE uid=?', (uid,))
            self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
            self.db.execute('UPDATE warnings SET count=0,mute_until=0 WHERE uid=?', (uid,))
        self.audit(actor, uid, 'unblacklist')

    def subscribed(self, uid):
        member = self.call('getChatMember', chat_id=self.channel, user_id=uid)
        return member['status'] in ('creator', 'administrator', 'member') or (
            member['status'] == 'restricted' and member.get('is_member', False))

    def restrict(self, uid, until=0):
        self.call('restrictChatMember', chat_id=self.group, user_id=uid,
                  permissions={key: False for key in PERMISSIONS}, use_independent_chat_permissions=True,
                  until_date=until)

    def restore(self, uid, clear_mute=False):
        row = self.db.execute('SELECT mute_until FROM warnings WHERE uid=?', (uid,)).fetchone()
        if not clear_mute and row and row['mute_until'] > time.time():
            self.restrict(uid, row['mute_until'])
            return
        permissions = self.call('getChat', chat_id=self.group)['permissions']
        self.call('restrictChatMember', chat_id=self.group, user_id=uid,
                  permissions={key: permissions.get(key, False) for key in PERMISSIONS},
                  use_independent_chat_permissions=True)

    def start_verification(self, user, kind, chat=None):
        uid = user['id']
        if self.blacklisted(uid):
            self.call('declineChatJoinRequest' if kind == 'request' else 'banChatMember', chat_id=self.group, user_id=uid)
            self.audit(0, uid, 'blacklist_reject')
            return
        if uid == self.bot_id or self.admin(uid):
            return
        if self.whitelisted(uid):
            if kind == 'request':
                try:
                    self.call('approveChatJoinRequest', chat_id=self.group, user_id=uid)
                except APIError as exc:
                    if exc.code != 400 or 'user_already_participant' not in exc.description.lower():
                        raise
            return
        if user.get('is_bot'):
            method = 'declineChatJoinRequest' if kind == 'request' else 'banChatMember'
            self.call(method, chat_id=self.group, user_id=uid)
            self.audit(0, uid, 'reject_bot')
            return
        existing = self.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()
        if existing and existing['expires'] > time.time() and existing['kind'] == kind:
            return
        if kind == 'member':
            self.restrict(uid)
        a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
        nonce = secrets.token_hex(6)
        chat = chat or uid
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO verification(uid,chat,kind,nonce,answer,expires) VALUES(?,?,?,?,?,?)',
                            (uid, chat, kind, nonce, a + b, int(time.time()) + self.verify_seconds))
        answers = [a+b, a+b+1, a+b+2, a+b-1]
        secrets.SystemRandom().shuffle(answers)
        keyboard = {'inline_keyboard': []}
        if self.channel_url:
            keyboard['inline_keyboard'].append([{'text': '① 关注频道', 'url': self.channel_url}])
        keyboard['inline_keyboard'].append(
            [{'text': str(n), 'callback_data': f'v:{uid}:{nonce}:{n}'} for n in answers])
        verify_time = f'{self.verify_seconds // 60} 分钟' if self.verify_seconds >= 60 else f'{self.verify_seconds} 秒'
        text = f'<a href="tg://user?id={uid}">新成员</a>，请先关注频道，再选择 {a} + {b} 的答案。\n验证限时 {verify_time}，最多答错 3 次。'
        try:
            sent = self.send(chat, text, reply_markup=keyboard, keep=True)
            if chat == self.group and isinstance(sent, dict) and sent.get('message_id'):
                with self.db:
                    self.db.execute('INSERT OR REPLACE INTO verification_prompts VALUES(?,?,?,?,?)',
                                    (chat, sent['message_id'], uid, nonce,
                                     self.db.execute('SELECT expires FROM verification WHERE uid=?', (uid,)).fetchone()[0]))
        except APIError as exc:
            if exc.code not in (400, 403):
                with self.db:
                    self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
                raise
            if kind == 'request':
                self.call('declineChatJoinRequest', chat_id=self.group, user_id=uid)
                with self.db:
                    self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
                self.audit(0, uid, 'verification_delivery_failed')
            else:
                raise

    def reject(self, row):
        uid = row['uid']
        if row['kind'] == 'request':
            try:
                self.call('declineChatJoinRequest', chat_id=self.group, user_id=uid)
            except APIError as exc:
                if exc.code != 400 or not any(s in exc.description.lower() for s in ('hide_requester_missing', 'user_already_participant')):
                    raise
        else:
            # Unban also removes current members; leave them free to request entry again.
            self.call('unbanChatMember', chat_id=self.group, user_id=uid, only_if_banned=False)
        with self.db:
            self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
        self.audit(0, uid, 'verification_failed')

    def callback(self, query):
        data = query.get('data', '').split(':')
        if len(data) != 4 or data[0] != 'v':
            return
        try:
            uid, answer = int(data[1]), int(data[3])
        except ValueError:
            return
        def reply(text):
            self.call('answerCallbackQuery', callback_query_id=query['id'], text=text, show_alert=True)
        if uid != query['from']['id']:
            reply('这是其他成员的验证，请完成你自己的验证。')
            return
        row = self.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()
        if not row or row['nonce'] != data[2] or row['expires'] < time.time():
            reply('验证已失效，请重新申请入群。')
            return
        if answer != row['answer']:
            with self.db:
                self.db.execute('UPDATE verification SET attempts=attempts+1 WHERE uid=?', (uid,))
            if row['attempts'] + 1 >= 3:
                self.reject(row)
                reply('答错已达 3 次，请重新申请入群。')
            else:
                reply('答案不正确，请重试。')
            return
        if not self.subscribed(uid):
            reply('尚未检测到关注频道。请关注后再次点击正确答案。')
            return
        # Persist authorization before approval: resulting member update must not re-challenge.
        with self.db:
            self.db.execute('UPDATE verification SET passed=1 WHERE uid=?', (uid,))
        if row['kind'] == 'request':
            try:
                self.call('approveChatJoinRequest', chat_id=self.group, user_id=uid)
            except APIError as exc:
                if exc.code != 400 or 'user_already_participant' not in exc.description.lower():
                    raise
        else:
            self.restore(uid)
        with self.db:
            self.db.execute('UPDATE verification SET kind=?,expires=? WHERE uid=?', ('approved', int(time.time()) + 86400, uid))
        self.audit(0, uid, 'verified')
        reply('验证成功，欢迎入群！')
        msg = query.get('message')
        if msg:
            try:
                self.call('editMessageText', chat_id=msg['chat']['id'], message_id=msg['message_id'], text='✅ 已通过频道关注与人机验证。')
            except APIError:
                LOG.info('Could not edit completed challenge')

    def warn(self, uid, event, reason, actor=0, silent=False):
        with self.db:
            previous = self.db.execute('SELECT * FROM violations WHERE event=?', (event,)).fetchone()
            if previous:
                if previous['done']:
                    return
                count = previous['count']
            else:
                self.db.execute('INSERT OR IGNORE INTO warnings(uid) VALUES(?)', (uid,))
                self.db.execute('UPDATE warnings SET count=count+1 WHERE uid=?', (uid,))
                count = self.db.execute('SELECT count FROM warnings WHERE uid=?', (uid,)).fetchone()['count']
                self.db.execute('INSERT INTO violations(event,uid,count,reason) VALUES(?,?,?,?)', (event, uid, count, reason))
        suffix = ''
        if count >= 3:
            if uid < 0:
                if not previous or not previous['punished']:
                    self.call('banChatSenderChat', chat_id=self.group, sender_chat_id=uid)
                suffix = '\n已禁止该频道在群内发言。'
            else:
                if not previous or not previous['punished']:
                    self.blacklist_member(uid, actor, f'警告累计 {count} 次：{reason}')
                suffix = '\n已移出本群、加入黑名单，并请求删除全部群消息。'
        if count >= 3:
            with self.db:
                self.db.execute('UPDATE violations SET punished=1 WHERE event=?', (event,))
        label = f'<a href="tg://user?id={uid}">该成员</a>' if uid > 0 else f'频道 {uid}'
        if not silent:
            self.send(self.group, f'⚠️ {label} 警告 {count}/3：{html.escape(reason)}{suffix}')
        with self.db:
            self.db.execute('UPDATE violations SET done=1 WHERE event=?', (event,))
        self.audit(actor, uid, 'warn', reason)

    def avatar_visible(self, uid):
        try:
            photos = self.call('getUserProfilePhotos',user_id=uid,offset=0,limit=1)
        except APIError:
            LOG.warning('Avatar check unavailable uid=%s',uid)
            return None
        if not isinstance(photos,dict) or not isinstance(photos.get('total_count'),int) or not isinstance(photos.get('photos'),list):
            return None
        return photos['total_count']>0 and bool(photos['photos'])

    def points_command(self, msg):
        text = msg.get('text', '').strip()
        if msg.get('chat', {}).get('type') == 'private':
            text = {'/start points':'/points','/start rank':'/rank',
                    '/start milestones':'/milestones'}.get(text,text)
        parts = text.split()
        if not parts:
            return False
        command = parts[0].split('@',1)
        if len(command)>1 and command[1].lower()!=self.username.lower():
            return False
        cmd = POINTS_COMMAND_ALIASES.get(command[0].lower(), command[0].lower())
        if cmd not in ('/checkin','/points','/rank','/milestones'):
            return False
        user = msg['from']
        if user.get('is_bot') or user['id']==self.bot_id:
            return True
        return self.handle_points_command(msg, cmd)

    def handle_points_command(self, msg, cmd):
        user = msg['from']
        if msg.get('chat', {}).get('type') == 'private':
            return self.private_ranking(msg, cmd)
        if cmd in ('/points','/rank','/milestones'):
            start = cmd[1:]
            self.send(self.group,'积分和排行榜请前往机器人私聊查看。',reply_markup={'inline_keyboard':[[{'text':'私聊查看','url':f'https://t.me/{self.username}?start={start}'}]]})
            return True
        pending = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (user['id'],)).fetchone()
        if pending and not self.whitelisted(user['id']):
            self.send(self.group,'请先完成入群验证，再使用积分功能。')
            return True
        if cmd == '/checkin':
            visible = self.avatar_visible(user['id'])
            if visible is not True:
                if visible is False:
                    self.warn(user['id'],f'avatar_checkin:{self.group}:{msg["message_id"]}',
                              '头像不可见时发起签到，本次不加分。请设置头像并允许所有人查看后重试。')
                return True
            amount, status = self.points.award(user,'checkin',f'checkin:{self.group}:{msg["message_id"]}',msg.get('date'))
            if status == 'added':
                self.upgrade_member(user['id'])
            total = self.points.stats(user['id'])[0]
            text = f'✅ 签到成功，获得 {amount} 积分！' if status!='limit' else f'今天已经签到过了。北京时间零点后可再次签到。'
        self.send(self.group,text,reply_parameters={'message_id':msg['message_id'],'allow_sending_without_reply':True})
        return True

    def private_ranking(self, msg, cmd):
        """Points details and full rankings are private-chat only."""
        chat = msg['chat']['id']
        if cmd not in ('/rank','/points','/milestones'):
            self.send(chat, '私聊支持积分、排行榜和达标榜：/points、/rank、/milestones。签到请在群内进行。')
            return True
        uid = msg['from']['id']
        root = getattr(self, 'manager_root', self)
        if root.group_manager_root and not msg.get('_selected_group'):
            groups = root.db.execute('SELECT chat,title FROM managed_groups WHERE enabled=1 ORDER BY title COLLATE NOCASE,chat').fetchall()
            if len(groups) > 1:
                command_code = {'/points': 'p', '/rank': 'r', '/milestones': 'g'}[cmd]
                buttons = [[{'text': f'{str(row["title"])[:34]} · {row["chat"]}',
                             'callback_data': f'pg:{command_code}:{row["chat"]}:{uid}'}]
                           for row in groups]
                root.send(chat, '已启用多个管理群，请选择要查看的群组数据：',
                          reply_markup={'inline_keyboard': buttons})
                return True
        group_title = str(self.group)
        if root.group_manager_root:
            title_row = root.db.execute('SELECT title FROM managed_groups WHERE chat=?', (self.group,)).fetchone()
            if title_row:
                group_title = title_row['title']
        safe_group_title = html.escape(group_title)
        try:
            member = self.call('getChatMember', chat_id=self.group, user_id=uid)
        except APIError:
            self.send(chat, '暂时无法核实群成员身份，请稍后重试。')
            return True
        active = member.get('status') in ('member','administrator','creator') or (
            member.get('status')=='restricted' and member.get('is_member'))
        if not active:
            self.send(chat, f'请先加入「{safe_group_title}」后再查询该群积分。')
            return True
        if cmd == '/points':
            total, checkin, messages, rank = self.points.stats(uid)
            self.send(chat,f'📍 群组：{safe_group_title}\n⭐ 我的积分：{total}\n今日签到：{str(checkin)+" 分" if checkin else "未签到"}\n今日发言积分：{messages}/{self.message_daily_limit}\n总榜排名：{rank or "暂无"}')
            return True
        if cmd == '/milestones':
            self.send(chat, f'📍 群组：{safe_group_title}\n{self.milestones_ranking()}')
        else:
            text, markup = self.ranking_page(uid, 0)
            self.send(chat, text, reply_markup=markup)
        return True

    def private_group_callback(self, query):
        """Route a private points/ranking query to the group chosen by the user."""
        fields = query.get('data', '').split(':')
        if len(fields) != 4 or fields[0] != 'pg':
            return False
        answer = lambda text='': self.call('answerCallbackQuery', callback_query_id=query['id'], text=text)
        if not self.group_manager_root:
            answer('群组选择已失效，请重新发送查询命令。')
            return True
        commands = {'p': '/points', 'r': '/rank', 'g': '/milestones'}
        try:
            cmd, gid, owner = commands[fields[1]], int(fields[2]), int(fields[3])
        except (KeyError, ValueError):
            answer('群组选择已失效，请重新发送查询命令。')
            return True
        message = query.get('message', {})
        chat = message.get('chat', {})
        if (query.get('from', {}).get('id') != owner or chat.get('type') != 'private'
                or chat.get('id') != owner):
            answer('请在自己的私聊中选择群组。')
            return True
        row = self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?', (gid,)).fetchone()
        tenant = self.managed_bots.get(gid)
        if not row or not row['enabled'] or tenant is None:
            answer('该群组已停用，请重新发送查询命令。')
            return True
        answer()
        try:
            tenant.private_ranking({'chat': chat, 'from': query['from'], '_selected_group': True}, cmd)
        finally:
            try:
                self.call('deleteMessage', chat_id=owner, message_id=message['message_id'])
            except (APIError, KeyError):
                pass
        return True

    def milestones_ranking(self):
        rows = self.db.execute('''SELECT p.name,m.ordinal FROM point_milestones m
            JOIN point_users p ON p.uid=m.uid WHERE m.threshold=?
            ORDER BY m.ordinal ASC LIMIT 50''',(self.upgrade_points,)).fetchall()
        lines = [f'🏆 达标榜 · 前 50 名（{len(rows)} 人）']
        if not rows:
            lines.append('榜单暂时为空。')
        for row in rows:
            name = html.escape(' '.join((row['name'] or '').split()).replace('@','＠'))
            lines.append(f'{row["ordinal"]}. {name}')
        return '\n'.join(lines)

    def ranking_page(self, uid, page):
        rows, total, page, pages = self.points.leaderboard_page(page)
        group_title = str(self.group)
        root = getattr(self, 'manager_root', self)
        if root.group_manager_root:
            title_row = root.db.execute('SELECT title FROM managed_groups WHERE chat=?', (self.group,)).fetchone()
            if title_row:
                group_title = title_row['title']
        lines = [f'🏆 {html.escape(group_title)} · 积分排行榜（全部 {total} 人）', f'第 {page + 1}/{pages} 页 · 每页 50 人']
        if not rows:
            lines.append('暂无积分记录，先在群里签到或发言吧。')
        for row in rows:
            name = ' '.join(row['name'].split()).replace('@', '＠')
            if len(name) > 20:
                name = name[:19] + '…'
            lines.append(f'{row["rank"]}. {html.escape(name)} — {row["total"]} 分')
        buttons = []
        if page > 0:
            buttons.append({'text': '⬅️ 上一页', 'callback_data': f'rank:{self.group}:{uid}:{page - 1}'})
        if page + 1 < pages:
            buttons.append({'text': '下一页 ➡️', 'callback_data': f'rank:{self.group}:{uid}:{page + 1}'})
        return '\n'.join(lines), {'inline_keyboard': [buttons] if buttons else []}

    def ranking_callback(self, query):
        data = query.get('data', '')
        if not data.startswith('rank:'):
            return False
        def answer(text=''):
            self.call('answerCallbackQuery', callback_query_id=query['id'], text=text)
        try:
            fields=data.split(':')
            if len(fields)==4:
                _, group_id, owner, page = fields
                if int(group_id)!=self.group: raise ValueError()
            else:
                _, owner, page = fields
            owner, page = int(owner), int(page)
        except ValueError:
            answer('无效的页码，请重新发送 /rank。')
            return True
        msg = query.get('message', {})
        chat = msg.get('chat', {})
        if chat.get('type') != 'private' or chat.get('id') != owner or query['from']['id'] != owner:
            answer('请在自己的私聊中查询排行榜。')
            return True
        try:
            member = self.call('getChatMember', chat_id=self.group, user_id=owner)
            active = member.get('status') in ('member', 'administrator', 'creator') or (
                member.get('status') == 'restricted' and member.get('is_member'))
            if not active:
                answer('请先加入管理群后再查询排行榜。')
                return True
            text, markup = self.ranking_page(owner, page)
            self.call('editMessageText', chat_id=owner, message_id=msg['message_id'],
                      text=text, parse_mode='HTML', reply_markup=markup)
        except APIError as exc:
            if 'message is not modified' not in str(exc).lower():
                answer('暂时无法刷新排行榜，请稍后重试。')
                return True
        answer()
        return True


    def ai_context(self, msg):
        """Recent group messages before the trigger, so the AI can resolve references."""
        try:
            return self.history.recent(msg['chat']['id'], exclude_id=msg.get('message_id'),
                                       limit=self.history.context_messages, conversation_only=True,
                                       before_id=msg.get('message_id'), topic_id=msg.get('message_thread_id'))
        except Exception:
            LOG.warning('History lookup failed chat=%s', msg.get('chat', {}).get('id'))
            return ''

    def bin_command(self, msg):
        parts = msg.get('text', '').split()
        if not parts:
            return False
        cmd = parts[0].split('@', 1)
        if cmd[0].lower() != '/bin' or (len(cmd)>1 and cmd[1].lower()!=self.username.lower()):
            return False
        chat = msg['chat']['id']
        if len(parts) != 2:
            result = '用法：/bin 45717360（输入 6–8 位 BIN）'
        else:
            try:
                result = format_result(parts[1], self.bin_lookup.lookup(parts[1]))
            except LookupError as exc:
                result = str(exc)
        self.send(chat, result, reply_parameters={'message_id':msg['message_id'], 'allow_sending_without_reply':True},
                  link_preview_options={'is_disabled':True})
        return True

    def help_command(self, msg):
        """展示当前群的动态帮助；积分、时长等说明取本群实际配置。"""
        parts = msg.get('text', '').split()
        if not parts:
            return False
        first = parts[0].split('@', 1)
        if first[0].lower() != '/help' or (len(first) > 1 and first[1].lower() != self.username.lower()):
            return False
        user = msg.get('from', {})
        if msg.get('sender_chat') or not user.get('id') or user.get('is_bot'):
            return True
        chat = msg['chat']['id']
        verify_time = f'{self.verify_seconds // 60} 分钟' if self.verify_seconds >= 60 else f'{self.verify_seconds} 秒'
        ai_cooldown = f'普通成员每 {self.ai_cooldown_seconds} 秒一次' if self.ai_cooldown_seconds else '普通成员不限频'
        everyone = ('<b>全员可用</b>\n'
                    f'• /checkin — 每日一次，随机 {self.checkin_min}–{self.checkin_max} 分；要求头像对机器人可见，未公开会提示并计警告\n'
                    f'• 发言积分：每次 {self.message_points} 分，每天最多 {self.message_daily_limit} 分；头像不可见时静默不计分\n'
                    '• /points — 私聊查看余额、今日积分与排名；多群时先选择群组\n'
                    '• /report — 回复本群消息，提交 Jev 广告判断；1 名管理员或 5 名群员同意后警告一次\n'
                    '• /rank — 私聊查看排行榜，每页 50 人，按钮翻页，不艾特榜单成员；多群时先选择群组\n'
                    f'• 达标榜：/milestones — 首次达到 {self.upgrade_points} 分的前 50 位，不要求特定标签；多群时先选择群组\n'
                    '• 多群管理：群主在新群发送 /group_add 登记，私聊 /groups 启用与切换；群数据独立\n'
                    '• BIN 查询：/bin 45717360 — 输入 6–8 位 BIN\n'
                    '• 空投：/airdrop 最低积分 [活跃分钟] [中奖人数] 奖品 — 普通成员每天可申请一次；/airdrops [编号] 查看最近 20 条或详情\n'
                    f'• AI 问答：/ai 你的问题；或回复文字、图片、相册发送 /ai — {ai_cooldown}，管理员不限\n'
                    f'• 入群验证：先关注关联频道，再完成算术题，{verify_time} 内最多答错 3 次\n'
                    '• 解封申请：被拉黑后私聊 /appeal 理由；也可先 /appeal 再填写，/cancel 取消填写；每天最多一次，待审不可重复提交')
        admins = ('<b>仅管理员</b>\n'
                  '• /manage [ID/@用户名] — 回复成员消息打开管理面板；查看警告、禁言、拉黑、解封和白名单，全部使用按钮\n'
                  '• /manage — 查看本群黑白名单，选择成员管理\n'
                  '• 空投审核：普通成员申请由任一群管理员同意或拒绝；管理员每日额度外的申请仍需群主批准\n'
                  '• 多群管理：群主在新群发送 /group_add 登记，私聊 /groups 启停群组、切换当前私聊群；数据各自独立\n'
                  '• /audit 最近 100 条全部日志；/audit ID 查看该用户日志；/status 运行状态；/help 查看此完整功能帮助')
        owner = ('<b>仅群主 · 私聊</b>\n'
                 '• /settings — 私聊选择群和配置项，直接发送新值，或 /cancel 取消\n'
                 '• /appeals — 查看含昵称提及、用户名和 ID 的待审申请，点击批准或拒绝；请先私聊 /start 以接收申请通知\n'
                 '• 批准后解除黑名单并清零警告，用户需重新申请入群；处理结果私聊通知申请人')
        automatic = (f'<b>自动规则</b>\n'
                     f'• 达到 {self.upgrade_points} 分发送「第 N 位达标」恭喜消息，30 秒后自动删除；已有自定义标签也会通知\n'
                     f'• 符合条件时升级为「{html.escape(self.upgrade_tag)}」，不覆盖已有标签\n'
                     '• 连续 3 个完整自然日每天签到、没有正常发言：拉黑并清零积分\n'
                     '• 广告举报仅通过回复消息发送 /report 触发；群内投票确认后处罚\n'
                     '• 用户命令源消息保留；普通机器人消息 30 秒后删除；积分与榜单结果仅私聊展示')
        footer = ('群内机器人操作不设每日次数上限；签到、空投额度和 AI 冷却仍按各自规则执行。\n'
                  '管理目标可回复消息指定，或填写数字 ID / 已知 @用户名；匿名管理员请切换个人身份。')
        text = f'🤖 <b>功能菜单 · 2026-10-05</b>\n\n{everyone}\n\n{admins}\n\n{owner}\n\n{automatic}\n\n{footer}'
        self.send(chat, text, reply_parameters={'message_id': msg['message_id'], 'allow_sending_without_reply': True},
                  link_preview_options={'is_disabled': True})
        return True

    def command(self, msg, update_id):
        """Public moderation entry; retired commands are deliberately not aliases."""
        words = msg.get('text', '').split()
        if not words:
            return False
        command = words[0].split('@', 1)
        if len(command) > 1 and command[1].lower() != self.username.lower():
            return False
        if command[0].lower() == '/manage':
            return self.moderation.command(msg)
        if command[0].lower() in ('/audit', '/status'):
            return self._moderation_action(msg, update_id)
        return False

    def _moderation_action(self, msg, update_id):
        """内部管理服务：面板调用旧动作解析器；不向用户注册独立管理命令。

        每次仍重新核验管理员和目标身份。公开入口只允许 /manage、/audit、/status。
        """
        text = msg.get('text', '')
        parts = text.split()
        if not parts:
            return False
        first = parts[0].split('@', 1)
        if len(first) > 1 and first[1].lower() != self.username.lower():
            return False
        cmd = first[0].lower()
        if cmd not in ('/warn', '/mute', '/unmute', '/resetwarn', '/ban', '/unban',
                       '/white', '/unwhite', '/status',
                       '/audit'):
            return False
        uid = msg.get('from', {}).get('id')
        # Anonymous/sender-chat commands cannot be attributed and are refused.
        if msg.get('sender_chat') or not uid or not self.admin(uid):
            return True
        if cmd == '/audit':
            args = parts[1:]
            target_filter = None
            if args:
                if len(args) != 1:
                    self.send(self.group, '用法：/audit 查看最近 100 条全部日志；/audit 用户ID或@用户名 查看该用户日志。')
                    return True
                try:
                    if args[0].isdigit():
                        target_filter = int(args[0])
                        if target_filter <= 0: raise ValueError()
                    elif args[0].startswith('@'):
                        target_filter = self.members.resolve(args[0])
                        if not target_filter: raise ValueError()
                    else: raise ValueError()
                except Exception:
                    self.send(self.group, '无法确定目标用户，请填写有效数字 ID 或已知 @用户名。')
                    return True
            limit = 20 if target_filter is not None else 100
            if target_filter is not None:
                rows = self.db.execute('SELECT * FROM audit WHERE target=? OR actor=? ORDER BY at DESC,id DESC LIMIT ?', (target_filter, target_filter, limit)).fetchall()
            else:
                rows = self.db.execute('SELECT * FROM audit ORDER BY at DESC,id DESC LIMIT ?', (limit,)).fetchall()
            if not rows:
                self.send(self.group, '暂无操作日志。' if target_filter is None else f'暂无涉及用户 {target_filter} 的操作日志。')
                return True
            heading = f'📜 最近 {limit} 条操作日志（实际 {len(rows)} 条）' + (f' · 用户 {target_filter}' if target_filter is not None else ' · 全部')
            chunks = []; lines = []; size = 0
            for row in rows:
                at = time.strftime('%m-%d %H:%M:%S', time.localtime(row['at']))
                actor = f'ID:{row["actor"]}' if row['actor'] else '系统'
                target = f'ID:{row["target"]}' if row['target'] else '系统/群组'
                line = f'• [{at}] {actor} 对 {target} 执行了 {html.escape(row["action"][:100])}' + (f' ({html.escape(row["detail"][:100])})' if row['detail'] else '')
                units = len(line.encode('utf-16-le')) // 2 + 1
                if lines and size + units > 3300:
                    chunks.append('\n'.join(lines)); lines = []; size = 0
                lines.append(line); size += units
            if lines: chunks.append('\n'.join(lines))
            for page, chunk in enumerate(chunks, 1):
                self.send(self.group, f'{heading} · {page}/{len(chunks)}\n{chunk}')
            return True
        if cmd == '/status':
            pending = self.db.execute("SELECT count(*) FROM verification WHERE kind!='approved'").fetchone()[0]
            banned = self.db.execute('SELECT count(*) FROM blacklist').fetchone()[0]
            family = self.db.execute("SELECT count(*) FROM upgrades WHERE tag=?", (self.upgrade_tag,)).fetchone()[0]
            tags = '正常' if self.can_manage_tags else '缺少 can_manage_tags 权限，已停用'
            white = self.db.execute('SELECT count(*) FROM whitelist').fetchone()[0]
            self.send(self.group, f'✅ 群管运行中\n待验证：{pending}\n黑名单：{banned}\n白名单：{white} 人\n标签升级：累计 {self.upgrade_points} 分改为「{self.upgrade_tag}」，已升级 {family} 人（{tags}）\n警告累计 3 次拉黑并删除消息。')
            return True
        args = parts[1:]
        replied = msg.get('reply_to_message', {})
        if args and args[0].startswith('@'):
            try:
                target = self.members.resolve(args.pop(0))
            except (ValueError, APIError) as exc:
                self.send(self.group, html.escape(str(exc)) if isinstance(exc, ValueError) else '暂时无法核实目标用户，未执行操作。')
                return True
        elif replied:
            if replied.get('sender_chat') or not replied.get('from'):
                self.send(self.group, '请回复以个人身份发送的成员消息。')
                return True
            target = replied['from']['id']
        elif args:
            try:
                target = self.members.resolve(args.pop(0))
            except (ValueError, APIError) as exc:
                self.send(self.group, html.escape(str(exc)) if isinstance(exc, ValueError) else '暂时无法核实目标用户，未执行操作。')
                return True
        else:
            self.send(self.group, '请回复成员消息执行，或提供数字用户 ID / @用户名。')
            return True
        if target == self.bot_id or self.admin(target):
            self.send(self.group, '不能对管理员或本机器人执行此操作。')
            return True
        if cmd == '/warn':
            self.warn(target, f'command:{update_id}', ' '.join(args) or '管理员手动警告', uid)
        elif cmd == '/mute':
            if args and (not args[0].isdigit() or not 1 <= int(args[0]) <= 525600):
                self.send(self.group, '禁言时长请输入 1–525600 分钟。')
                return True
            seconds = int(args[0]) * 60 if args else self.mute_seconds
            until = int(time.time()) + seconds
            self.restrict(target, until)
            with self.db:
                self.db.execute('INSERT INTO warnings(uid,mute_until) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET mute_until=excluded.mute_until', (target, until))
            self.send(self.group, f'已禁言用户 {target}，时长 {seconds // 60} 分钟。')
            self.audit(uid, target, 'mute', str(seconds))
        elif cmd == '/unmute':
            pending = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (target,)).fetchone()
            if pending:
                self.send(self.group, '该成员尚未完成入群验证，请先完成验证。')
                return True
            self.restore(target, clear_mute=True)
            with self.db:
                self.db.execute('UPDATE warnings SET mute_until=0 WHERE uid=?', (target,))
            self.send(self.group, f'已解除用户 {target} 的禁言。')
            self.audit(uid, target, 'unmute')
        elif cmd == '/resetwarn':
            with self.db:
                self.db.execute('UPDATE warnings SET count=0 WHERE uid=?', (target,))
            self.send(self.group, f'已清除用户 {target} 的警告次数；现有禁言不受影响。')
            self.audit(uid, target, 'resetwarn')
        elif cmd == '/ban':
            try:
                self.blacklist_member(target, uid, ' '.join(args) or '管理员手动封禁')
            except APIError as exc:
                self.send(self.group, f'封禁用户 {target} 失败（{exc.code} {exc.description}），已保留黑名单记录。')
                return True
            self.send(self.group, f'🚫 已将用户 {target} 封禁并移出本群、加入黑名单，已请求 Telegram 删除其全部群消息。')
        elif cmd == '/unban':
            if not self.blacklisted(target):
                self.send(self.group, f'用户 {target} 不在黑名单中。')
                return True
            self.unblacklist_member(target, uid)
            self.send(self.group, f'已解除用户 {target} 的黑名单并清零警告，可重新申请入群。')
        elif cmd == '/white':
            if self.blacklisted(target):
                self.send(self.group, f'用户 {target} 在黑名单中，黑名单优先。如需加白名单请先在 /manage 面板解封。')
                return True
            note = ' '.join(args)[:100]
            pending = self.db.execute("SELECT kind FROM verification WHERE uid=? AND kind!='approved'", (target,)).fetchone()
            with self.db:
                self.db.execute('INSERT INTO whitelist(uid,at,actor,note) VALUES(?,?,?,?) '
                                'ON CONFLICT(uid) DO UPDATE SET note=excluded.note,actor=excluded.actor',
                                (target, int(time.time()), uid, note))
                self.db.execute("UPDATE verification SET passed=1,kind='approved',expires=? WHERE uid=? AND kind!='approved'",
                                (int(time.time()) + 86400, target))
            if pending:
                try:
                    if pending['kind'] == 'member':
                        self.restore(target)
                    elif pending['kind'] == 'request':
                        self.call('approveChatJoinRequest', chat_id=self.group, user_id=target)
                except APIError as exc:
                    if pending['kind'] != 'request' or exc.code != 400 or 'user_already_participant' not in exc.description.lower():
                        LOG.warning('Whitelist restore failed uid=%s: %s', target, exc)
            self.send(self.group, f'✅ 已将用户 {target} 加入白名单：豁免广告检测、三日自动黑名单、AI 冷却与入群验证。')
            self.audit(uid, target, 'whitelist', note)
        elif cmd == '/unwhite':
            if not self.db.execute('SELECT 1 FROM whitelist WHERE uid=?', (target,)).fetchone():
                self.send(self.group, f'用户 {target} 不在白名单中。')
                return True
            with self.db:
                self.db.execute('DELETE FROM whitelist WHERE uid=?', (target,))
            self.send(self.group, f'已移除用户 {target} 的白名单，恢复正常限制。')
            self.audit(uid, target, 'unwhitelist')
        return True

    def joined(self, user):
        row = self.db.execute('SELECT * FROM verification WHERE uid=?', (user['id'],)).fetchone()
        if row and row['passed'] and row['expires'] > time.time():
            return
        self.start_verification(user, 'member', self.group)

    def handle(self, update):
        msg = update.get('message') or update.get('edited_message') or {}
        self.ai.media.observe(msg)
        command_words=msg.get('text','').strip().split()
        if self.group_manager_root and command_words and command_words[0].split('@')[0].lower() == '/group_add':
            self.add_managed_group(msg)
            return
        self.members.observe(update)
        if 'callback_query' in update:
            if self.moderation.callback(update['callback_query']):
                return
            if self.appeals.callback(update['callback_query']):
                return
            if self.jev.callback(update['callback_query']):
                return
            if self.ranking_callback(update['callback_query']):
                return
            if self.airdrops.callback(update['callback_query']):
                return
            self.callback(update['callback_query'])
        elif 'chat_join_request' in update:
            req = update['chat_join_request']
            if req['chat']['id'] == self.group:
                if self.blacklisted(req['from']['id']):
                    self.call('declineChatJoinRequest', chat_id=self.group, user_id=req['from']['id'])
                    self.audit(0, req['from']['id'], 'blacklist_join_request')
                    return
                self.start_verification(req['from'], 'request', req['user_chat_id'])
        elif 'chat_member' in update:
            event = update['chat_member']
            if event['chat']['id'] != self.group:
                return
            old, new = event['old_chat_member'], event['new_chat_member']
            active = lambda m: m['status'] in ('creator','administrator','member') or (m['status']=='restricted' and m.get('is_member',False))
            if not active(old) and active(new):
                if self.blacklisted(new['user']['id']):
                    # Invited around the join request: remove again and keep them out.
                    self.call('banChatMember', chat_id=self.group, user_id=new['user']['id'])
                    self.audit(0, new['user']['id'], 'blacklist_rejoin')
                    return
                self.joined(new['user'])
            elif active(old) and not active(new):
                with self.db:
                    self.db.execute('DELETE FROM verification WHERE uid=?', (new['user']['id'],))
        else:
            msg = update.get('message') or update.get('edited_message')
            if not msg:
                return
            if 'message' in update and self.appeals.command(msg):
                return
            if 'message' in update and self.jev.command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.help_command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.ai.command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.airdrops.command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.points_command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.bin_command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.command(msg, update['update_id']):
                return
            if msg['chat']['id'] != self.group:
                if msg['chat'].get('type') == 'private' and msg.get('text', '').startswith('/start'):
                    self.send(msg['chat']['id'], '请从群组入口申请加入，然后完成本机器人发送的频道关注与人机验证。\nhttps://t.me/setupode')
                return
        # sender_chat identifies channel posts even when Telegram also includes a bot-like from field.
            sender = msg.get('sender_chat')
            if not sender and msg.get('from', {}).get('is_bot') and msg.get('message_id'):
                # Telegram only exposes some other-bot messages; queue the ones we do see.
                self.schedule_delete(msg['chat']['id'], msg['message_id'])
            for user in msg.get('new_chat_members', []):
                self.joined(user)
            if msg.get('is_automatic_forward'):
                return
            if 'edited_message' in update:
                self.jev.invalidate_edit(msg)
            if sender:
                return
            if not msg.get('from'):
                return
            uid = msg['from']['id']
            if uid == self.bot_id:
                return
            self.history.record(msg)
            if not msg['from'].get('is_bot'):
                with self.db:
                    self.db.execute('INSERT INTO user_activity(uid,last_active) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET last_active=excluded.last_active', (uid, int(msg.get('date') or time.time())))
            if 'message' in update and not self.whitelisted(uid):
                self.attention.observed(msg)
            if 'message' in update and self.help_command(msg):
                return
            if 'message' in update and self.ai.command(msg):
                return
            if 'message' in update and self.airdrops.command(msg):
                return
            if 'message' in update and self.points_command(msg):
                return
            if 'message' in update and self.bin_command(msg):
                return
            if 'message' in update and self.command(msg, update['update_id']):
                return
            uid = msg['from']['id']
            if uid == self.bot_id or msg['from'].get('is_bot'):
                return
            pending = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (uid,)).fetchone()
            if pending and not self.whitelisted(uid):
                return
            content = any(msg.get(key) for key in ('text','photo','video','animation','audio','voice','video_note','document','sticker','poll','contact','location','venue','dice'))
            if 'message' in update and content and not msg.get('text','').startswith('/') and self.avatar_visible(uid) is True:
                amount, status = self.points.award(msg['from'],'message',f'message:{self.group}:{msg["message_id"]}',msg.get('date'))
                if status == 'added':
                    self.upgrade_member(uid)

    def expire(self):
        rows = self.db.execute("SELECT * FROM verification WHERE expires<? AND kind!='approved'", (int(time.time()),)).fetchall()
        for row in rows:
            if self.whitelisted(row['uid']):
                with self.db:
                    self.db.execute("UPDATE verification SET passed=1,kind='approved',expires=? WHERE uid=?",
                                    (int(time.time()) + 86400, row['uid']))
                try:
                    if row['kind'] == 'member':
                        self.restore(row['uid'])
                    elif row['kind'] == 'request':
                        self.call('approveChatJoinRequest', chat_id=self.group, user_id=row['uid'])
                except APIError as exc:
                    if row['kind'] != 'request' or exc.code != 400 or 'user_already_participant' not in exc.description.lower():
                        LOG.warning('Whitelist verification restore failed uid=%s: %s', row['uid'], exc)
                continue
            try:
                self.reject(row)
            except APIError as exc:
                LOG.warning('Expiry failed uid=%s: %s', row['uid'], exc)

    def _init_group_manager(self):
        """Load independently stored group profiles; one poller owns the bot token."""
        self.group_manager_root = True
        self.managed_bots = {self.group: self}
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS managed_groups(
          chat INTEGER PRIMARY KEY,channel INTEGER NOT NULL,title TEXT NOT NULL,
          db_path TEXT NOT NULL,enabled INTEGER NOT NULL DEFAULT 0,
          channel_url TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS group_setting_sessions(
          uid INTEGER PRIMARY KEY,group_id INTEGER NOT NULL,key TEXT NOT NULL,
          prompt_message_id INTEGER NOT NULL,panel_message_id INTEGER NOT NULL,
          expires INTEGER NOT NULL);
        ''')
        root_path = str(Path(self.cfg.get('DB_PATH', 'bot.sqlite3')).resolve())
        try:
            root_info = self.call('getChat', chat_id=self.group)
            title = root_info.get('title') or str(self.group)
        except Exception:
            title = str(self.group)
        existing_root = self.db.execute('SELECT channel FROM managed_groups WHERE chat=?',
                                        (self.group,)).fetchone()
        root_channel = int(existing_root['channel']) if existing_root else self.channel
        try:
            root_channel_info = self.call('getChat', chat_id=root_channel)
        except Exception:
            root_channel_info = {}
        root_fallback = self.channel_url if root_channel == self.channel else ''
        root_channel_url = self._channel_url_from_chat(root_channel_info, root_fallback)
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO managed_groups(chat,channel,title,db_path,enabled,channel_url) VALUES(?,?,?,?,1,?)',
                            (self.group, self.channel, title, root_path, root_channel_url))
            self.db.execute('UPDATE managed_groups SET channel_url=? WHERE chat=? AND channel_url=\'\'',
                            (root_channel_url, self.group))
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('active_group',?)", (str(self.group),))
        rows = self.db.execute('SELECT * FROM managed_groups ORDER BY chat').fetchall()
        for row in rows:
            channel_url = row['channel_url'] or ''
            if not channel_url:
                try:
                    channel_info = self.call('getChat', chat_id=row['channel'])
                except Exception:
                    channel_info = {}
                fallback = self.channel_url if row['channel'] == self.channel else ''
                channel_url = self._channel_url_from_chat(channel_info, fallback)
                if channel_url:
                    with self.db:
                        self.db.execute('UPDATE managed_groups SET channel_url=? WHERE chat=?',
                                        (channel_url, row['chat']))
            if row['chat'] == self.group:
                self.channel = int(row['channel'])
                self.cfg['CHANNEL_ID'] = str(self.channel)
                self.channel_url = channel_url
                self.cfg['TELEGRAM_CHANNEL_URL'] = channel_url
                continue
            tenant_cfg = dict(self.base_cfg) | {'GROUP_ID': str(row['chat']), 'CHANNEL_ID': str(row['channel']),
                                           'DB_PATH': row['db_path'],
                                           'TELEGRAM_CHANNEL_URL': channel_url}
            tenant = Bot(tenant_cfg, api=self.api)
            tenant.username, tenant.bot_id = self.username, self.bot_id
            tenant.manager_root = self
            self.managed_bots[row['chat']] = tenant
        active = self.db.execute("SELECT value FROM meta WHERE key='active_group'").fetchone()
        if not active or int(active[0]) not in self.managed_bots:
            with self.db: self.db.execute("INSERT OR REPLACE INTO meta VALUES('active_group',?)", (str(self.group),))

    def _group_admin(self, uid):
        for group in self.managed_bots.values():
            try:
                member = group.call('getChatMember', chat_id=group.group, user_id=uid)
                if member.get('status') in ('creator', 'administrator'):
                    return True
            except Exception:
                continue
        return False

    def _settings_owner_status(self, tenant, uid):
        try:
            return tenant.call('getChatMember', chat_id=tenant.group, user_id=uid).get('status') == 'creator'
        except APIError:
            return None

    def _setting_display(self, key, value):
        if key == 'JEV_ENABLED':
            return '开启' if value else '关闭'
        return str(value) if value != '' else '未设置'

    def _settings_panel(self, tenant, title, uid):
        row = self.db.execute('SELECT channel,channel_url FROM managed_groups WHERE chat=?',
                              (tenant.group,)).fetchone()
        channel_id = row['channel'] if row else tenant.channel
        channel_url = (row['channel_url'] if row else tenant.channel_url) or ''
        lines = [f'⚙️ <b>{html.escape(title)}</b>（<code>{tenant.group}</code>）设置',
                 '点击下方项目修改；数值设置请直接发送新值，或用 /cancel 取消。']
        range_text = f'{tenant.checkin_min}–{tenant.checkin_max}'
        lines.append(f'• 签到积分范围：<b>{range_text}</b>（每天一次）')
        for key, spec in tenant.GROUP_SETTING_SPECS.items():
            if key in ('CHECKIN_MIN', 'CHECKIN_MAX'):
                continue
            value = html.escape(self._setting_display(key, tenant.setting_values[key]))
            origin = '自定义' if key in tenant.setting_overrides else '默认'
            lines.append(f'• {html.escape(spec["label"])}：<b>{value}</b> · {origin}')
        escaped_url = html.escape(channel_url, quote=True)
        channel_text = f'<a href="{escaped_url}">{html.escape(channel_url)}</a>' if channel_url else '未设置'
        lines.extend([f'• 入群验证频道：<code>{channel_id}</code>',
                      f'• 关注频道链接：{channel_text}',
                      '', '机器人令牌、AI API 密钥和 API 地址由部署环境管理。'])

        values = [('CHECKIN_RANGE', '签到积分范围', range_text)]
        for key, spec in tenant.GROUP_SETTING_SPECS.items():
            if key in ('CHECKIN_MIN', 'CHECKIN_MAX'):
                continue
            values.append((key, spec['label'], self._setting_display(key, tenant.setting_values[key])))
        rows = []
        pair = []
        for key, label, value in values:
            action = 't' if key == 'JEV_ENABLED' else 'e'
            value_label = '开' if key == 'JEV_ENABLED' and tenant.setting_values[key] else (
                '关' if key == 'JEV_ENABLED' else str(value))
            button = {'text': f'{label} · {value_label}'[:60],
                      'callback_data': f'gs:{action}:{tenant.group}:{uid}:{key}'}
            pair.append(button)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([
            {'text': '入群验证频道', 'callback_data': f'gs:e:{tenant.group}:{uid}:CHANNEL_ID'},
            {'text': '频道邀请链接', 'callback_data': f'gs:e:{tenant.group}:{uid}:CHANNEL_URL'},
        ])
        resettable = []
        if tenant.setting_overrides.intersection({'CHECKIN_MIN', 'CHECKIN_MAX'}):
            resettable.append({'text': '签到范围恢复默认',
                               'callback_data': f'gs:r:{tenant.group}:{uid}:CHECKIN_RANGE'})
        for key in tenant.GROUP_SETTING_SPECS:
            if key in ('CHECKIN_MIN', 'CHECKIN_MAX') or key not in tenant.setting_overrides:
                continue
            resettable.append({'text': f'恢复{tenant.GROUP_SETTING_SPECS[key]["label"][:14]}',
                               'callback_data': f'gs:r:{tenant.group}:{uid}:{key}'})
        for index in range(0, len(resettable), 2):
            rows.append(resettable[index:index + 2])
        return '\n'.join(lines), {'inline_keyboard': rows}

    def _settings_title(self, gid):
        row = self.db.execute('SELECT title FROM managed_groups WHERE chat=?', (gid,)).fetchone()
        return row['title'] if row else str(gid)

    def _show_settings_panel(self, uid, tenant, message=None):
        text, markup = self._settings_panel(tenant, self._settings_title(tenant.group), uid)
        if message:
            self.call('editMessageText', chat_id=message['chat']['id'],
                      message_id=message['message_id'], text=text, parse_mode='HTML',
                      reply_markup=markup, disable_web_page_preview=True)
        else:
            self.send(uid, text, keep=True, reply_markup=markup, disable_web_page_preview=True)

    def _settings_group_picker(self, uid, owned, message=None):
        lines = ['⚙️ <b>选择要修改的管理群</b>']
        buttons = []
        for tenant, title in owned:
            lines.append(f'• {html.escape(title)}（<code>{tenant.group}</code>）')
            buttons.append([{'text': title[:50], 'callback_data': f'gs:g:{tenant.group}:{uid}'}])
        markup = {'inline_keyboard': buttons}
        text = '\n'.join(lines)
        if message:
            self.call('editMessageText', chat_id=message['chat']['id'],
                      message_id=message['message_id'], text=text, parse_mode='HTML',
                      reply_markup=markup)
        else:
            self.send(uid, text, keep=True, reply_markup=markup)

    def group_settings_command(self, msg):
        """Open private button-based settings for groups owned by the caller."""
        chat = msg.get('chat', {})
        if chat.get('type') != 'private':
            return False
        text = msg.get('text', '').strip()
        tokens = text.split(maxsplit=1)
        if not tokens:
            return False
        command = tokens[0].split('@', 1)[0].lower()
        if command != '/settings':
            return False
        uid = msg.get('from', {}).get('id')
        if not uid:
            return True
        if len(tokens) != 1:
            self.send(chat['id'], '设置现已改为按钮操作，请直接发送 /settings 并选择管理群。')
            return True
        owned = []
        for row in self.db.execute('SELECT chat,title FROM managed_groups ORDER BY title COLLATE NOCASE,chat').fetchall():
            tenant = self.managed_bots.get(row['chat'])
            if tenant and self._settings_owner_status(tenant, uid):
                owned.append((tenant, row['title']))
        if not owned:
            self.send(chat['id'], '只有已登记管理群的群主可以查看和修改群组设置。')
        elif len(owned) == 1:
            self._show_settings_panel(uid, owned[0][0])
        else:
            self._settings_group_picker(uid, owned)
        return True

    def _settings_value_prompt(self, uid, tenant, key, panel_message, error=''):
        prompts = {
            'CHECKIN_RANGE': '请直接发送签到积分范围，例如 5-10（每天仍限签到一次）。',
            'CHANNEL_ID': '请直接发送该群绑定的频道 @用户名 或频道数字 ID。机器人必须是该频道管理员。',
            'CHANNEL_URL': '请直接发送关注频道链接（https://t.me/...），支持公开频道和私有邀请链接。',
        }
        spec = tenant.GROUP_SETTING_SPECS.get(key)
        prompt = prompts.get(key)
        if not prompt and spec:
            prompt = f'请直接发送“{spec["label"]}”的新值。'
            if spec['type'] == 'int':
                prompt += f' 范围：{spec["min"]}–{spec["max"]}。'
            elif spec['type'] == 'tag':
                prompt += ' 限 1–16 个 UTF-16 字符，不能含 Emoji。'
        response = self.send(uid, (html.escape(error) + '\n' if error else '') + prompt + '\n直接发送新值，或用 /cancel 取消（10 分钟内有效）。',
                             keep=True)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO group_setting_sessions'
                            '(uid,group_id,key,prompt_message_id,panel_message_id,expires) VALUES(?,?,?,?,?,?)',
                            (uid, tenant.group, key, response['message_id'], panel_message['message_id'],
                             int(time.time()) + 600))

    def _valid_channel_url(self, raw):
        value = raw.strip()
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in ('t.me', 'telegram.me') or
                parsed.username or parsed.password or parsed.port or not parsed.path.strip('/') or
                parsed.query or parsed.fragment or any(char.isspace() for char in value)):
            raise ValueError('请提供有效的 https://t.me/... 频道链接。')
        return 'https://' + parsed.netloc.lower() + parsed.path

    def _save_tenant_setting(self, tenant, key, value):
        with tenant.db:
            tenant.db.execute('INSERT OR REPLACE INTO group_settings(key,value) VALUES(?,?)',
                              (key, '1' if value is True else '0' if value is False else str(value)))
        tenant.setting_values[key] = value
        tenant.setting_overrides.add(key)
        tenant._apply_group_settings()

    def _reset_tenant_setting(self, tenant, key):
        keys = ('CHECKIN_MIN', 'CHECKIN_MAX') if key == 'CHECKIN_RANGE' else (key,)
        with tenant.db:
            tenant.db.executemany('DELETE FROM group_settings WHERE key=?', ((item,) for item in keys))
        for item in keys:
            tenant.setting_values[item] = tenant.setting_defaults[item]
            tenant.setting_overrides.discard(item)
        tenant._apply_group_settings()

    def _settings_refresh_message(self, uid, tenant, message):
        try:
            self._show_settings_panel(uid, tenant, message)
        except APIError as exc:
            # Telegram may reject an edit when the panel already has identical content.
            if exc.code != 400 or 'message is not modified' not in exc.description.lower():
                raise

    def group_settings_callback(self, query):
        fields = query.get('data', '').split(':')
        if len(fields) < 4 or fields[0] != 'gs':
            return False
        answer = lambda text='': self.call('answerCallbackQuery',
                                           callback_query_id=query['id'], text=text,
                                           show_alert=bool(text))
        try:
            action = fields[1]
            if action == 'g' and len(fields) == 4:
                gid, uid = int(fields[2]), int(fields[3])
                key = ''
            elif action in ('e', 't', 'r') and len(fields) == 5:
                gid, uid, key = int(fields[2]), int(fields[3]), fields[4]
            else:
                answer('设置按钮已失效，请重新发送 /settings。')
                return True
        except ValueError:
            answer('设置按钮无效，请重新发送 /settings。')
            return True
        msg = query.get('message', {})
        caller = query.get('from', {}).get('id')
        if msg.get('chat', {}).get('type') != 'private' or caller != uid:
            answer('此设置按钮不属于你。')
            return True
        tenant = self.managed_bots.get(gid)
        if not tenant or not self.db.execute('SELECT 1 FROM managed_groups WHERE chat=?', (gid,)).fetchone():
            answer('该管理群已不存在。')
            return True
        status = self._settings_owner_status(tenant, uid)
        if status is None:
            answer('暂时无法核实你的群主身份。')
            return True
        if not status:
            answer('只有该群群主可以修改设置。')
            return True
        if action == 'g':
            self._settings_refresh_message(uid, tenant, msg)
            answer()
            return True
        if action == 't':
            if key != 'JEV_ENABLED':
                answer('设置按钮无效。')
                return True
            value = not tenant.setting_values[key]
            self._save_tenant_setting(tenant, key, value)
            tenant.audit(uid, 0, 'group_setting_set', f'{key}={value}')
            self._settings_refresh_message(uid, tenant, msg)
            answer()
            return True
        if action == 'r':
            if key == 'CHECKIN_RANGE':
                self._reset_tenant_setting(tenant, key)
            elif key in tenant.GROUP_SETTING_SPECS and key in tenant.setting_overrides:
                self._reset_tenant_setting(tenant, key)
            else:
                answer('此项目没有自定义值可恢复。')
                return True
            tenant.audit(uid, 0, 'group_setting_reset', key)
            self._settings_refresh_message(uid, tenant, msg)
            answer()
            return True
        if action == 'e':
            allowed = set(tenant.GROUP_SETTING_SPECS) - {'JEV_ENABLED', 'CHECKIN_MIN', 'CHECKIN_MAX'}
            allowed.update(('CHECKIN_RANGE', 'CHANNEL_ID', 'CHANNEL_URL'))
            if key not in allowed:
                answer('设置按钮无效。')
                return True
            self._settings_value_prompt(uid, tenant, key, msg)
            answer()
            return True
        return True

    def group_settings_reply(self, msg):
        """接收群主的新参数；会话绑定用户和群，取消、过期和身份变化均不得写入。"""
        if msg.get('chat', {}).get('type') != 'private':
            return False
        uid = msg.get('from', {}).get('id')
        if not uid:
            return False
        session = self.db.execute('SELECT * FROM group_setting_sessions WHERE uid=?', (uid,)).fetchone()
        if not session:
            return False
        text = (msg.get('text') or '').strip()
        if text.split(maxsplit=1)[0].split('@')[0].lower() == '/cancel' if text else False:
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.send(uid, '已取消本次设置。')
            return True
        if int(session['expires']) < time.time():
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            return False
        tenant = self.managed_bots.get(session['group_id'])
        if not tenant or self._settings_owner_status(tenant, uid) is not True:
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.send(uid, '群主身份已变化，设置操作已取消。')
            return True
        key = session['key']
        if key not in tenant.GROUP_SETTING_SPECS and key not in ('CHECKIN_RANGE', 'CHANNEL_ID', 'CHANNEL_URL'):
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.send(uid, '此设置已移除，请重新发送 /settings。')
            return True
        if text.startswith('/') or not text:
            self.send(uid, '请直接发送新参数，或用 /cancel 取消。')
            return True
        try:
            if key == 'CHECKIN_RANGE':
                match = re.fullmatch(r'\s*(\d+)\s*[-–]\s*(\d+)\s*', text)
                if not match:
                    raise ValueError('签到积分范围格式示例：5-10。')
                low = tenant.parse_group_setting('CHECKIN_MIN', match.group(1))
                high = tenant.parse_group_setting('CHECKIN_MAX', match.group(2))
                if low > high:
                    raise ValueError('签到下限不能高于上限。')
                self._save_tenant_setting(tenant, 'CHECKIN_MIN', low)
                self._save_tenant_setting(tenant, 'CHECKIN_MAX', high)
                audit_value = f'{low}-{high}'
            elif key == 'CHANNEL_ID':
                info = self.call('getChat', chat_id=text)
                if info.get('type') != 'channel':
                    raise ValueError('该 ID/用户名不是频道。')
                membership = self.call('getChatMember', chat_id=info['id'], user_id=self.bot_id)
                if membership.get('status') not in ('administrator', 'creator'):
                    raise ValueError('机器人必须先成为该频道管理员。')
                link = self._channel_url_from_chat(info, '')
                with self.db:
                    self.db.execute('UPDATE managed_groups SET channel=?,channel_url=? WHERE chat=?',
                                    (info['id'], link, tenant.group))
                tenant.channel = int(info['id'])
                tenant.channel_url = link
                tenant.cfg['CHANNEL_ID'] = str(info['id'])
                tenant.cfg['TELEGRAM_CHANNEL_URL'] = link
                audit_value = str(info['id'])
            elif key == 'CHANNEL_URL':
                value = self._valid_channel_url(text)
                with self.db:
                    self.db.execute('UPDATE managed_groups SET channel_url=? WHERE chat=?',
                                    (value, tenant.group))
                tenant.channel_url = value
                tenant.cfg['TELEGRAM_CHANNEL_URL'] = value
                audit_value = value
            else:
                value = tenant.parse_group_setting(key, text)
                self._save_tenant_setting(tenant, key, value)
                audit_value = str(value)
        except (ValueError, APIError, KeyError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else '读取或验证频道失败，请检查频道 ID 和机器人权限后重试。'
            self._settings_value_prompt(uid, tenant, key,
                                        {'message_id': session['panel_message_id']}, error)
            return True
        with self.db:
            self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
        tenant.audit(uid, 0, 'group_setting_set', f'{key}={audit_value}')
        panel_msg = {'chat': msg['chat'], 'message_id': session['panel_message_id']}
        self._settings_refresh_message(uid, tenant, panel_msg)
        return True

    def add_managed_group(self, msg):
        chat = msg.get('chat', {})
        uid = msg.get('from', {}).get('id')
        if chat.get('type') not in ('group', 'supergroup') or not uid or msg.get('sender_chat'):
            return
        gid = int(chat['id'])
        def notice(text): self.api.call('sendMessage', chat_id=gid, text=text)
        if gid in self.managed_bots:
            notice('这个群已经登记。群主私聊机器人发送 /groups 可启用或切换当前管理群。')
            return
        try:
            creator = self.call('getChatMember', chat_id=gid, user_id=uid)
            bot_member = self.call('getChatMember', chat_id=gid, user_id=self.bot_id)
            if creator.get('status') != 'creator':
                notice('只有本群群主可以登记该群。')
                return
            if bot_member.get('status') not in ('administrator', 'creator'):
                notice('请先把机器人设为本群管理员，再发送 /group_add。')
                return
            for right in ('can_delete_messages', 'can_restrict_members', 'can_invite_users'):
                if bot_member.get('status') != 'creator' and not bot_member.get(right):
                    notice('机器人缺少群管理权限，请授予删消息、限制成员和邀请用户权限后重试。')
                    return
            parts = msg.get('text', '').split(maxsplit=1)
            channel = self.channel
            if len(parts) > 1:
                channel_info = self.call('getChat', chat_id=parts[1].strip())
                if channel_info.get('type') != 'channel':
                    notice('请指定 Telegram 频道的 @用户名 或数字 ID。')
                    return
                channel = channel_info['id']
            channel_info = self.call('getChat', chat_id=channel)
            if channel_info.get('type') != 'channel':
                notice('关联对象不是 Telegram 频道。')
                return
            channel_url = self._channel_url_from_chat(channel_info, '')
            channel_member = self.call('getChatMember', chat_id=channel, user_id=self.bot_id)
            if channel_member.get('status') not in ('administrator','creator'):
                notice('机器人还不是关联频道管理员，请先授予频道管理员权限后重试。')
                return
            title = chat.get('title') or self.call('getChat', chat_id=gid).get('title') or str(gid)
            root_db = Path(self.cfg.get('DB_PATH', 'bot.sqlite3')).resolve()
            group_db = root_db.parent / 'groups' / str(abs(gid)) / 'bot.sqlite3'
            group_db.parent.mkdir(parents=True, exist_ok=True)
            cfg = dict(self.base_cfg) | {'GROUP_ID': str(gid), 'CHANNEL_ID': str(channel), 'DB_PATH': str(group_db),
                                    'TELEGRAM_CHANNEL_URL': channel_url}
            tenant = Bot(cfg, api=self.api)
            tenant.username, tenant.bot_id = self.username, self.bot_id
            tenant.manager_root = self
            tenant.can_manage_tags = bool(bot_member.get('status') == 'creator' or bot_member.get('can_manage_tags'))
            with self.db:
                self.db.execute('INSERT INTO managed_groups(chat,channel,title,db_path,enabled,channel_url) VALUES(?,?,?,?,0,?)',
                                (gid, channel, title, str(group_db), channel_url))
            self.managed_bots[gid] = tenant
            notice('✅ 群组已登记。群主私聊机器人发送 /groups，启用该群并将其设为当前管理群。')
        except Exception as exc:
            LOG.warning('Group registration failed chat=%s type=%s', gid, type(exc).__name__)
            notice('登记失败，请确认机器人在关联频道和本群的权限后重试。')

    def group_manager_command(self, msg):
        text = msg.get('text', '').strip()
        cmd = text.split(maxsplit=1)[0].split('@')[0].lower() if text else ''
        if cmd != '/groups' or msg.get('chat', {}).get('type') != 'private':
            return False
        uid = msg.get('from', {}).get('id')
        if not uid or not self._group_admin(uid):
            self.send(msg['chat']['id'], '仅已管理群组的管理员可以查看和切换群配置。')
            return True
        rows = self.db.execute('SELECT * FROM managed_groups ORDER BY chat').fetchall()
        active_row = self.db.execute("SELECT value FROM meta WHERE key='active_group'").fetchone()
        active = int(active_row[0]) if active_row else self.group
        lines = ['🛠 管理群组（每个群的数据独立保存）']
        buttons = []
        for row in rows:
            state = '已启用' if row['enabled'] else '已停用'
            current = ' · 当前私聊管理群' if row['chat'] == active else ''
            lines.append(f'• {html.escape(row["title"])}（{row["chat"]}）— {state}{current}')
            buttons.append([
                {'text': ('停用' if row['enabled'] else '启用') + f' · {row["title"][:18]}',
                 'callback_data': f'mg:toggle:{row["chat"]}'},
                {'text': '设为当前', 'callback_data': f'mg:active:{row["chat"]}'}])
        lines += ['', '在新群先把机器人设为管理员，再由群主在群内发送 /group_add 登记。']
        self.send(msg['chat']['id'], '\n'.join(lines), reply_markup={'inline_keyboard': buttons})
        return True

    def group_manager_callback(self, query):
        data = query.get('data', '').split(':')
        if len(data) != 3 or data[0] != 'mg' or data[1] not in ('toggle', 'active'):
            return False
        try: gid = int(data[2])
        except ValueError: return True
        answer = lambda text: self.call('answerCallbackQuery', callback_query_id=query['id'], text=text, show_alert=True)
        uid = query.get('from', {}).get('id')
        if not uid or not self._group_admin(uid):
            answer('仅已管理群组的管理员可以操作。'); return True
        row = self.db.execute('SELECT * FROM managed_groups WHERE chat=?', (gid,)).fetchone()
        if not row or gid not in self.managed_bots:
            answer('群组不存在，请重新发送 /groups。'); return True
        if data[1] == 'toggle':
            enabled = 0 if row['enabled'] else 1
            if enabled:
                tenant = self.managed_bots[gid]
                try:
                    member = tenant.call('getChatMember', chat_id=gid, user_id=self.bot_id)
                    if member.get('status') not in ('administrator', 'creator'):
                        answer('机器人已不是该群管理员，无法启用。'); return True
                    if member.get('status') != 'creator':
                        missing = [right for right in ('can_delete_messages','can_restrict_members','can_invite_users') if not member.get(right)]
                        if missing:
                            answer('机器人缺少必要管理权限，无法启用。'); return True
                    channel_member=tenant.call('getChatMember',chat_id=tenant.channel,user_id=self.bot_id)
                    if channel_member.get('status') not in ('administrator','creator'):
                        answer('机器人还不是关联频道管理员，无法启用。'); return True
                    tenant.can_manage_tags = bool(member.get('status') == 'creator' or member.get('can_manage_tags'))
                except Exception:
                    answer('暂时无法核实机器人权限。'); return True
            with self.db: self.db.execute('UPDATE managed_groups SET enabled=? WHERE chat=?', (enabled, gid))
            if not enabled and int(self._active_group()) == gid:
                replacement=self.db.execute('SELECT chat FROM managed_groups WHERE enabled=1 AND chat!=? ORDER BY chat LIMIT 1',(gid,)).fetchone()
                if replacement:
                    with self.db:self.db.execute("INSERT OR REPLACE INTO meta VALUES('active_group',?)",(str(replacement['chat']),))
            answer('已启用该群机器人功能。' if enabled else '已停用该群机器人功能。')
        else:
            if not row['enabled']:
                answer('请先启用该群。'); return True
            with self.db: self.db.execute("INSERT OR REPLACE INTO meta VALUES('active_group',?)", (str(gid),))
            answer('已切换当前私聊管理群。')
        msg = query.get('message', {})
        try:
            self.call('editMessageText', chat_id=msg['chat']['id'], message_id=msg['message_id'],
                      text='群配置已更新。请再次发送 /groups 查看状态。')
        except Exception: pass
        return True

    def _tenant_for_update(self, update):
        """将更新路由到对应群，避免不同群的积分和管理操作串用。"""
        if 'callback_query' in update:
            query = update['callback_query']; data = query.get('data', '')
            if data.startswith('mg:'): return self
            message_chat = query.get('message', {}).get('chat', {})
            if message_chat.get('id') in self.managed_bots:
                state=self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?',(message_chat['id'],)).fetchone()
                return self.managed_bots[message_chat['id']] if state and state['enabled'] else None
            uid = query.get('from', {}).get('id')
            if data.startswith('v:'):
                try: nonce = data.split(':')[2]; uid = int(data.split(':')[1])
                except (ValueError, IndexError): pass
                for tenant in self.managed_bots.values():
                    enabled=self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?',(tenant.group,)).fetchone()
                    if enabled and enabled['enabled'] and tenant.db.execute('SELECT 1 FROM verification WHERE uid=? AND nonce=?', (uid, nonce)).fetchone(): return tenant
            if data.startswith('appeal:'):
                fields=data.split(':')
                if len(fields)==4:
                    try:
                        tenant=self.managed_bots.get(int(fields[2]))
                        state=self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?',(int(fields[2]),)).fetchone()
                        return tenant if tenant and state and state['enabled'] else None
                    except ValueError:return None
                try: aid = int(data.rsplit(':',1)[1])
                except ValueError: aid = -1
                for tenant in self.managed_bots.values():
                    if tenant.db.execute('SELECT 1 FROM appeals WHERE id=? AND state="pending"', (aid,)).fetchone(): return tenant
            if data.startswith('mod:'):
                fields = data.split(':')
                if len(fields) == 4:
                    try:
                        gid = int(fields[1])
                        tenant = self.managed_bots.get(gid)
                        state = self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?', (gid,)).fetchone()
                        return tenant if tenant and state and state['enabled'] else None
                    except ValueError:
                        return None
                return None
            if data.startswith('rank:'):
                fields=data.split(':')
                if len(fields)==4:
                    try:
                        gid=int(fields[1]);tenant=self.managed_bots.get(gid)
                        state=self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?',(gid,)).fetchone()
                        return tenant if tenant and state and state['enabled'] else None
                    except ValueError:pass
            tenant=self._active_tenant()
            if tenant:return tenant
            self._no_active_group(query.get('from',{}).get('id'))
            return None
        msg = update.get('message') or update.get('edited_message')
        if msg:
            chat = msg.get('chat', {})
            gid = chat.get('id')
            if chat.get('type') != 'private' and gid not in self.managed_bots:
                words=msg.get('text','').split()
                return self if words and words[0].split('@')[0].lower() == '/group_add' else None
            if chat.get('type') != 'private' and gid in self.managed_bots:
                row = self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?', (gid,)).fetchone()
                return self.managed_bots[gid] if row and row['enabled'] else None
            if chat.get('type') == 'private':
                words=msg.get('text','').split()
                if words and words[0].split('@')[0].lower() == '/groups': return self
                if words and words[0].split('@')[0].lower() == '/appeal':
                    uid=msg.get('from',{}).get('id'); matches=[]
                    if len(words)>1 and words[1].lstrip('-').isdigit():
                        chosen=self.managed_bots.get(int(words[1]))
                        if chosen:
                            msg['text']='/appeal'+((' '+' '.join(words[2:])) if len(words)>2 else '')
                            return chosen
                    for tenant in self.managed_bots.values():
                        if tenant.blacklisted(uid):matches.append(tenant)
                    if len(matches)==1:return matches[0]
                    if len(matches)>1:
                        self.send(msg['chat']['id'],'你在多个管理群的黑名单中。请发送 /appeal 群ID 理由，群 ID 可向群管理员询问。')
                        return None
                tenant=self._active_tenant()
                if tenant:return tenant
                self._no_active_group(chat.get('id'))
                return None
        for key in ('chat_join_request','chat_member','my_chat_member'):
            if key in update:
                gid=update[key].get('chat',{}).get('id')
                if gid in self.managed_bots:
                    row=self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?',(gid,)).fetchone()
                    return self.managed_bots[gid] if row and row['enabled'] else None
        return None

    def _active_group(self):
        row=self.db.execute("SELECT value FROM meta WHERE key='active_group'").fetchone()
        return row[0] if row else self.group

    def _active_tenant(self):
        try: gid=int(self._active_group())
        except (TypeError,ValueError): return None
        state=self.db.execute('SELECT enabled FROM managed_groups WHERE chat=?',(gid,)).fetchone()
        return self.managed_bots.get(gid) if state and state['enabled'] else None

    def _no_active_group(self, uid):
        if uid:
            try:self.api.call('sendMessage',chat_id=uid,text='当前没有启用的管理群。群组管理员私聊发送 /groups 启用一个群。')
            except Exception:pass

    def _validate_tenant(self, tenant):
        channel_member=tenant.call('getChatMember',chat_id=tenant.channel,user_id=self.bot_id)
        if channel_member.get('status') not in ('administrator','creator'):
            raise RuntimeError(f'Bot must be administrator in channel {tenant.channel}')
        member = tenant.call('getChatMember', chat_id=tenant.group, user_id=self.bot_id)
        if member.get('status') not in ('administrator','creator'):
            raise RuntimeError(f'Bot must be administrator in group {tenant.group}')
        if member.get('status') != 'creator':
            for right in ('can_delete_messages','can_restrict_members','can_invite_users'):
                if not member.get(right): raise RuntimeError(f'Missing administrator right {right} in group {tenant.group}')
        tenant.can_manage_tags = bool(member.get('status') == 'creator' or member.get('can_manage_tags'))

    def run(self):
        me = self.call('getMe')
        self.username, self.bot_id = me['username'], me['id']
        if self.call('getWebhookInfo').get('url'):
            raise RuntimeError('Existing webhook configured; refusing to replace it')
        self._init_group_manager()
        for row in self.db.execute('SELECT chat,enabled FROM managed_groups').fetchall():
            if row['enabled']:
                tenant=self.managed_bots[row['chat']]
                try:
                    self._validate_tenant(tenant)
                except Exception as exc:
                    if tenant.group == self.group:
                        raise
                    with self.db:self.db.execute('UPDATE managed_groups SET enabled=0 WHERE chat=?',(tenant.group,))
                    LOG.error('Disabled group=%s at startup: %s',tenant.group,exc)
                    continue
                if not tenant.can_manage_tags:
                    LOG.warning('Bot lacks can_manage_tags in group=%s; member tag upgrades are disabled', tenant.group)
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('start_time',?)", (str(int(time.time())),))
        start_time = int(self.db.execute("SELECT value FROM meta WHERE key='start_time'").fetchone()[0])
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        offset = int(row[0]) if row else 0
        LOG.info('Started @%s managed_groups=%s active_group=%s', self.username,
                 sum(1 for r in self.db.execute('SELECT enabled FROM managed_groups') if r['enabled']), self._active_group())
        while True:
            try:
                enabled = [self.managed_bots[r['chat']] for r in self.db.execute('SELECT chat FROM managed_groups WHERE enabled=1').fetchall()]
                for tenant in enabled:
                    tenant.expire()
                    tenant.ai.drain()
                    tenant.sweep_deletions()
                    tenant.history.prune()
                    tenant.jev.work()
                    tenant.attention.tick()
                    tenant.appeals.tick()
                    tenant.milestones.tick()
                # Short polls while anything is queued so removals land near their deadline.
                quick = any(bool(tenant.ai.futures) or tenant.jev.busy() or tenant.pending_deletions() for tenant in enabled)
                updates = self.call('getUpdates', offset=offset, timeout=1 if quick else 20,
                                    allowed_updates=['message', 'edited_message', 'chat_member', 'chat_join_request', 'callback_query', 'my_chat_member'])
                for update in updates:
                    payload = next((update[k] for k in ('edited_message','message','chat_member','chat_join_request','my_chat_member') if k in update), {})
                    event_time = payload.get('edit_date', payload.get('date', start_time))
                    if event_time >= start_time and not self.db.execute('SELECT 1 FROM processed WHERE id=?', (update['update_id'],)).fetchone():
                        try:
                            if 'callback_query' in update and update['callback_query'].get('data','').startswith('gs:'):
                                self.group_settings_callback(update['callback_query'])
                            elif 'callback_query' in update and update['callback_query'].get('data','').startswith('pg:'):
                                self.private_group_callback(update['callback_query'])
                            elif 'callback_query' in update and update['callback_query'].get('data','').startswith('mg:'):
                                self.group_manager_callback(update['callback_query'])
                            elif ((update.get('message') or {}).get('chat',{}).get('type')=='private'
                                  and ((update.get('message') or {}).get('text','').split()[:1] or [''])[0].split('@')[0].lower()=='/groups'):
                                self.group_manager_command(update['message'])
                            elif 'message' in update and self.group_settings_reply(update['message']):
                                pass
                            elif ((update.get('message') or {}).get('chat',{}).get('type')=='private'
                                  and ((update.get('message') or {}).get('text','').split()[:1] or [''])[0].split('@')[0].lower()
                                  == '/settings'):
                                self.group_settings_command(update['message'])
                            else:
                                tenant=self._tenant_for_update(update)
                                if tenant is not None:
                                    tenant.handle(update)
                        except APIError as exc:
                            if exc.code not in (400, 403):
                                raise
                            # A stale join/callback or deleted user must not block every later update.
                            LOG.error('Permanent failure update=%s: %s', update['update_id'], exc)
                            self.audit(0, 0, 'update_failed', f'{update["update_id"]}: {exc}')
                    offset = update['update_id'] + 1
                    with self.db:
                        self.db.execute('INSERT OR IGNORE INTO processed VALUES(?)', (update['update_id'],))
                        self.db.execute("INSERT OR REPLACE INTO meta VALUES('offset',?)", (str(offset),))
                    LOG.info('Processed update=%s', update['update_id'])
                if len(updates)<100:
                    self.attention.tick()
                    self.appeals.tick()
                    self.milestones.tick()
            except APIError as exc:
                LOG.warning('%s', exc)
                time.sleep(min(max(exc.retry_after, 3), 60))
            except Exception:
                LOG.exception('Update failed; will retry')
                time.sleep(5)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    Bot(config()).run()
