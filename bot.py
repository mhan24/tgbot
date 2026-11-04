#!/usr/bin/env python3
"""Telegram 群管：单群验证、管理服务与持久化消息清理。仅使用标准库。"""
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
from points import Points, INVITE_DAILY_LIMIT, format_checkin
from retention import NOTICE_SECONDS
from attention import Attention
from members import Members
from jev_ads import JevAds
from moderation import Moderation
from exchange import Exchange, RateError
from invitations import Invitations

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
            raise APIError(method, {'error_code':503,'description': 'Network failure'}) from None
        except (ValueError, UnicodeError):
            raise APIError(method, {'error_code':503,'description':'Invalid API response'}) from None
        if not isinstance(result, dict):
            raise APIError(method, {'error_code':503,'description':'Invalid API response'})
        if not result.get('ok'):
            raise APIError(method, result)
        if 'result' not in result:
            raise APIError(method, {'error_code':503,'description':'Missing API result'})
        return result['result']




class Bot:
    GROUP_SETTING_SPECS = {
        'MESSAGE_POINTS': {'type': 'int', 'min': 1, 'max': 100, 'label': '每条发言积分'},
        'MESSAGE_DAILY_LIMIT': {'type': 'int', 'min': 0, 'max': 1000, 'label': '每日发言积分上限'},
        'MUTE_SECONDS': {'type': 'int', 'min': 60, 'max': 2592000, 'label': '管理员默认禁言秒数'},
        'VERIFY_SECONDS': {'type': 'int', 'min': 30, 'max': 300, 'label': '入群验证秒数'},
        'DELETE_AFTER_SECONDS': {'type': 'int', 'min': 0, 'max': 3600, 'label': '群内机器人消息自动删除秒数'},
        'JEV_ENABLED': {'type': 'bool', 'label': 'Jev 手动广告举报'},
        'UPGRADE_POINTS': {'type': 'int', 'min': 1, 'max': 1000000, 'label': '成员标签/达标积分门槛'},
        'UPGRADE_TAG': {'type': 'tag', 'label': '达标后设置的成员标签'},
    }

    def __init__(self, cfg, api=None, db=None):
        self.cfg = dict(cfg)
        self.api = api or API(cfg['TELEGRAM_BOT_TOKEN'])
        self.group = int(cfg.get('GROUP_ID', -1003014905211))
        self.group_title = str(cfg.get('GROUP_TITLE') or self.group)
        self.channel = int(cfg['CHANNEL_ID'])
        self.channel_url = self._configured_channel_url(cfg)
        self.mute_seconds = int(cfg.get('MUTE_SECONDS', 86400))
        self.verify_seconds = int(cfg.get('VERIFY_SECONDS', 240))
        self.upgrade_points = int(cfg.get('UPGRADE_POINTS', 50))
        self.upgrade_tag = cfg.get('UPGRADE_TAG', '家人')
        # Seconds before the bot removes its own group messages and other bots' group messages.
        self.delete_after = int(cfg.get('DELETE_AFTER_SECONDS', 30))
        self.message_points = int(cfg.get('MESSAGE_POINTS', 1))
        self.message_daily_limit = int(cfg.get('MESSAGE_DAILY_LIMIT', 5))
        self.setting_defaults = {
            'MESSAGE_POINTS': self.message_points,
            'MESSAGE_DAILY_LIMIT': self.message_daily_limit,
            'MUTE_SECONDS': self.mute_seconds,
            'VERIFY_SECONDS': self.verify_seconds,
            'DELETE_AFTER_SECONDS': self.delete_after,
            # Only the canonical 1/0 and on/off tokens are accepted from now on.
            'JEV_ENABLED': str(cfg.get('JEV_ENABLED', '1')).strip().lower() in ('1', 'on'),
            'UPGRADE_POINTS': self.upgrade_points,
            'UPGRADE_TAG': self.upgrade_tag,
        }
        self.setting_defaults = {key: self.parse_group_setting(key, '1' if value is True else '0' if value is False else value)
                                 for key, value in self.setting_defaults.items()}
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
        CREATE TABLE IF NOT EXISTS private_steps(uid INTEGER,flow TEXT,message INTEGER,expires INTEGER,PRIMARY KEY(uid,flow,message));
        CREATE TABLE IF NOT EXISTS verification_prompts(chat INTEGER,message_id INTEGER,uid INTEGER,nonce TEXT,expires INTEGER,PRIMARY KEY(chat,message_id));
        CREATE TABLE IF NOT EXISTS whitelist(uid INTEGER PRIMARY KEY,at INTEGER,actor INTEGER,note TEXT);
        CREATE TABLE IF NOT EXISTS user_activity(uid INTEGER PRIMARY KEY, last_active INTEGER);
        CREATE TABLE IF NOT EXISTS group_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS group_setting_sessions(
          uid INTEGER PRIMARY KEY,group_id INTEGER NOT NULL,key TEXT NOT NULL,
          prompt_message_id INTEGER NOT NULL,panel_message_id INTEGER NOT NULL,expires INTEGER NOT NULL);
        ''')
        for key, attribute in (('verification_channel', 'channel'), ('verification_channel_url', 'channel_url')):
            row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
            if row:
                setattr(self, attribute, int(row[0]) if attribute == 'channel' else row[0])
        self._load_group_settings()
        self._apply_group_settings()
        self.bin_lookup = BinLookup(self.db)
        self.exchange = Exchange(self.db)
        self.points = Points(self.db, self.message_points, self.message_daily_limit)
        self.invitations = Invitations(self)
        self.attention = Attention(self)
        self.members = Members(self)
        self.jev = JevAds(self)
        self.moderation = Moderation(self)
        self.appeals = Appeals(self)
        self.milestones = Milestones(self)
        self.username = ''
        self.bot_id = 0

    def _load_group_settings(self):
        """Load only current settings; obsolete or malformed rows are deleted on startup.

        The current schema is authoritative: settings left behind by removed features
        and values that no longer parse are dropped instead of being carried forward.
        """
        stale = []
        for setting in self.db.execute('SELECT key,value FROM group_settings').fetchall():
            try:
                value = self.parse_group_setting(setting['key'], setting['value'])
            except (KeyError, ValueError):
                stale.append(setting['key'])
                continue
            self.setting_values[setting['key']] = value
            self.setting_overrides.add(setting['key'])
        if stale:
            with self.db:
                self.db.executemany('DELETE FROM group_settings WHERE key=?', ((key,) for key in stale))
            LOG.warning('Removed obsolete group settings group=%s keys=%s',
                        self.group, ','.join(sorted(stale)))

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
            values = {'1': True, 'on': True, '0': False, 'off': False}
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
        self.message_points = values['MESSAGE_POINTS']
        self.message_daily_limit = values['MESSAGE_DAILY_LIMIT']
        self.mute_seconds = values['MUTE_SECONDS']
        self.verify_seconds = values['VERIFY_SECONDS']
        self.delete_after = values['DELETE_AFTER_SECONDS']
        self.upgrade_points = values['UPGRADE_POINTS']
        self.upgrade_tag = values['UPGRADE_TAG']
        if hasattr(self, 'points'):
            self.points.message_points = self.message_points
            self.points.message_daily_limit = self.message_daily_limit

    def group_setting_value(self, key):
        value = self.setting_values[key]
        if isinstance(value, bool):
            return '开启' if value else '关闭'
        return str(value) if value != '' else '未设置'

    def call(self, method, **kwargs):
        keep = kwargs.pop('_keep', False)
        delete_after = kwargs.pop('_delete_after', None)
        if method == 'banChatMember':
            kwargs['revoke_messages'] = True
        try:
            result = self.api.call(method, **kwargs)
        except APIError as exc:
            if method=='answerCallbackQuery' and exc.code==400 and any(term in exc.description.lower() for term in ('query is too old','query_id_invalid','query id is invalid')):
                LOG.info('Callback acknowledgement expired; business state retained')
                return False
            raise
        if method == 'getChatMember' and isinstance(result, dict):
            self.members.remember(result.get('user'))
        if method == 'sendMessage' and isinstance(result, dict):
            if delete_after is not None or not keep:
                self.schedule_delete(kwargs.get('chat_id'), result.get('message_id'), seconds=delete_after)
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
        return cmd in ('/help', '/checkin', '/points', '/rank', '/milestones', '/bin', '/rate')

    def schedule_delete(self, chat, message_id, seconds=None, allow_private=False):
        delay = self.delete_after if seconds is None else seconds
        if delay <= 0 or chat is None or not message_id or (chat != self.group and not (allow_private and isinstance(chat, int) and chat > 0)):
            return
        with self.db:
            self.db.execute('INSERT OR IGNORE INTO deletions(chat,message_id,attempts,due) VALUES(?,?,0,?)',
                            (chat, int(message_id), int(time.time()) + delay))

    def finish_private_steps(self, uid, flow):
        # Only explicitly recorded bot prompts are eligible; user messages never enter this table.
        rows = self.db.execute('SELECT message FROM private_steps WHERE uid=? AND flow=?', (uid,flow)).fetchall()
        with self.db:
            for row in rows:
                self.schedule_delete(uid, row['message'], seconds=30, allow_private=True)
            self.db.execute('DELETE FROM private_steps WHERE uid=? AND flow=?', (uid,flow))

    def track_private_step(self, uid, message, flow, lifetime=3600, replace=True):
        if replace:
            self.finish_private_steps(uid, flow)
        with self.db:
            self.db.execute('INSERT INTO private_steps VALUES(?,?,?,?)', (uid,flow,message,int(time.time())+lifetime if lifetime is not None else None))

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
        for prompt in self.db.execute('SELECT uid,flow FROM private_steps WHERE expires<=?', (int(time.time()),)).fetchall():
            self.finish_private_steps(prompt['uid'], prompt['flow'])
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
        return self.call('sendMessage', chat_id=chat, text=text, parse_mode='HTML', _keep=keep, **kwargs)

    def notice_seconds(self):
        """Explicit retention for failure and punishment notices; 0 follows the group setting."""
        return NOTICE_SECONDS if self.delete_after > 0 else 0

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
                if not ((exc.code==400 and any(s in exc.description.lower() for s in ('hide_requester_missing','user_already_participant','user_id_invalid'))) or (exc.code==403 and 'user is deactivated' in exc.description.lower())):
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
            self.send(self.group, f'⚠️ {label} 警告 {count}/3：{html.escape(reason)}{suffix}',
                      _delete_after=self.notice_seconds())
        with self.db:
            self.db.execute('UPDATE violations SET done=1 WHERE event=?', (event,))
        self.audit(actor, uid, 'warn', reason)

    def avatar_visible(self, uid, message_id=None, purpose='diagnostic'):
        # Only bounded metadata is logged; photo/file IDs and response bodies stay private.
        def record(result, reason, count=None, returned=None, api_code=None, chat_photo=None, chat_status='not_checked'):
            LOG.info('Avatar check group=%s uid=%s purpose=%s message_id=%s total_count=%s returned=%s result=%s reason=%s api_code=%s chat_photo=%s chat_status=%s',
                     self.group, uid, purpose, message_id, count, returned,
                     'visible' if result is True else 'invisible' if result is False else 'unknown', reason, api_code, chat_photo, chat_status)
            return result
        try:
            photos = self.call('getUserProfilePhotos', user_id=uid, offset=0, limit=1)
        except APIError as exc:
            LOG.warning('Avatar check unavailable uid=%s', uid)
            return record(None, 'api_error', api_code=exc.code if isinstance(exc.code, int) else None)
        count = photos.get('total_count') if isinstance(photos, dict) else None
        groups = photos.get('photos') if isinstance(photos, dict) else None
        safe_count = count if type(count) is int and count >= 0 else None
        returned = len(groups) if isinstance(groups, list) else None
        if safe_count is None or returned is None:
            return record(None, 'malformed_response', safe_count, returned)
        if (count > 0) != bool(groups):
            return record(None, 'inconsistent_response', safe_count, returned)
        if count > 0:
            return record(True, 'has_photo', safe_count, returned)
        # The profile-photo list may be empty while the private chat exposes a photo.
        try:
            chat = self.call('getChat', chat_id=uid)
        except APIError as exc:
            return record(None, 'chat_lookup_error', safe_count, returned,
                          api_code=exc.code if isinstance(exc.code, int) else None, chat_status='api_error')
        if not isinstance(chat, dict) or chat.get('id') != uid or chat.get('type') != 'private':
            return record(None, 'chat_malformed_response', safe_count, returned, chat_status='malformed')
        photo = chat.get('photo')
        if photo is None:
            return record(False, 'empty_response', safe_count, returned, chat_photo=False, chat_status='ok')
        if not isinstance(photo, dict) or not any(isinstance(photo.get(key), str) and photo[key] for key in ('small_file_id', 'big_file_id')):
            return record(None, 'chat_malformed_photo', safe_count, returned, chat_status='malformed')
        return record(True, 'chat_photo', safe_count, returned, chat_photo=True, chat_status='ok')

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
            self.send(self.group,'请先完成入群验证，再使用积分功能。',
                      _delete_after=self.notice_seconds())
            return True
        if cmd == '/checkin':
            visible = self.avatar_visible(user['id'], message_id=msg.get('message_id'), purpose='checkin')
            if visible is not True:
                if visible is False:
                    self.warn(user['id'],f'avatar_checkin:{self.group}:{msg["message_id"]}',
                              '头像不可见时发起签到，本次不加分。请设置头像并允许所有人查看后重试。')
                else:
                    self.send(self.group, '暂时无法核实头像，本次未加分、未计警告。请稍后重新发送 /checkin。',
                              _delete_after=self.notice_seconds(),
                              reply_parameters={'message_id': msg['message_id'], 'allow_sending_without_reply': True})
                return True
            event = f'checkin:{self.group}:{msg["message_id"]}'
            amount, status, results, pity4, pity5, guaranteed = self.points.checkin(
                user, event, msg.get('date'))
            if status in ('added', 'replay') and amount:
                self.upgrade_member(user['id'])
            if status == 'limit':
                text = '今天已经签到过了。北京时间零点后可再次签到。'
                self.send(self.group, text, _delete_after=self.notice_seconds(), reply_parameters={
                    'message_id': msg['message_id'], 'allow_sending_without_reply': True})
                return True
            if not self.points.checkin_notified(event, 'result'):
                self.send(self.group, format_checkin(amount, results, pity4, pity5, guaranteed),
                          reply_parameters={'message_id': msg['message_id'],
                                            'allow_sending_without_reply': True})
                self.points.mark_checkin_notified(event, 'result')
            if any(rarity in (5, 6) for rarity in results or []) and not self.points.checkin_notified(event, 'broadcast'):
                entry = self.points.gold_entry(user['id'])
                self.send(self.group, self._checkin_broadcast(user, amount, results, entry))
                self.points.mark_checkin_notified(event, 'broadcast')
            return True
        return True

    def _checkin_broadcast(self, user, amount, results, entry=None):
        standard, up = results.count(5), results.count(6)
        total = standard + up
        name = ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip()
        name = name or user.get('username') or str(user['id'])
        mention = f'<a href="tg://user?id={user["id"]}">{html.escape(name)}</a>'
        if total == 1:
            tier = '五星 UP' if up else '五星常驻'
            text = f'🎉 {mention} 十连签到出现{tier}，获得 {amount} 积分！'
        else:
            tier = '三黄' if total >= 3 else '双黄'
            parts = []
            if up:
                parts.append(f'五星 UP ×{up}')
            if standard:
                parts.append(f'五星常驻 ×{standard}')
            text = f'🎉 {mention} 十连签到出现{tier}（{"、".join(parts)}），获得 {amount} 积分！'
        if entry:
            if entry['golds'] == total:
                text += f'\n🌟 第 {entry["ordinal"]} 位出金，已进入出金榜。'
            else:
                text += f'\n🌟 出金榜第 {entry["ordinal"]} 位，累计出金 {entry["golds"]} 次。'
        return text

    def private_ranking(self, msg, cmd):
        """Points details and full rankings are private-chat only."""
        chat = msg['chat']['id']
        if cmd not in ('/checkin','/rank','/points','/milestones'):
            self.send(chat, '私聊支持积分、排行榜和出金榜：/points、/rank、/milestones。签到请在群内进行。')
            return True
        uid = msg['from']['id']
        group_title = self.group_title
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
        if cmd == '/checkin':
            self.send(chat, f'📍 群组：{safe_group_title}\n{self.points.checkin_summary(uid)}')
            return True
        if cmd == '/points':
            total, checkin, messages, rank = self.points.stats(uid)
            self.send(chat,f'📍 群组：{safe_group_title}\n⭐ 我的积分：{total}\n今日签到：{str(checkin)+" 分" if checkin else "未签到"}\n今日发言积分：{messages}/{self.message_daily_limit}\n今日邀请积分：{self.points.invite_daily(uid)}/{INVITE_DAILY_LIMIT}\n总榜排名：{rank or "暂无"}')
            return True
        if cmd == '/milestones':
            text, markup = self.gold_ranking(uid, 0)
            self.send(chat, f'📍 群组：{safe_group_title}\n{text}', reply_markup=markup)
        else:
            text, markup = self.ranking_page(uid, 0)
            self.send(chat, text, reply_markup=markup)
        return True

    def gold_ranking(self, uid, page):
        """金榜：任何五星都上榜，按首次出金时间排序，不设门槛和人数上限。"""
        rows, total, page, pages = self.points.gold_leaderboard(page)
        lines = [f'🏆 {html.escape(self.group_title)} · 出金榜（共 {total} 人）',
                 f'第 {page + 1}/{pages} 页 · 每页 50 人']
        if not rows:
            lines.append('还没有成员出金。群内发送 /checkin，抽出五星即可上榜。')
        for row in rows:
            name = ' '.join((row['name'] or '').split()).replace('@', '＠')
            if len(name) > 20:
                name = name[:19] + '…'
            day = row['first_day']
            lines.append(f'{row["ordinal"]}. {html.escape(name)} — 首次出金 {day} · 累计 {row["golds"]} 次')
        buttons = []
        if page > 0:
            buttons.append({'text': '⬅️ 上一页', 'callback_data': f'gold:{self.group}:{uid}:{page - 1}'})
        if page + 1 < pages:
            buttons.append({'text': '下一页 ➡️', 'callback_data': f'gold:{self.group}:{uid}:{page + 1}'})
        return '\n'.join(lines), {'inline_keyboard': [buttons] if buttons else []}

    def ranking_page(self, uid, page):
        rows, total, page, pages = self.points.leaderboard_page(page)
        group_title = self.group_title
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
        kind = data.split(':', 1)[0]
        if kind not in ('rank', 'gold'):
            return False
        page_command = '/rank' if kind == 'rank' else '/milestones'
        def answer(text=''):
            self.call('answerCallbackQuery', callback_query_id=query['id'], text=text)
        try:
            _, group_id, owner, page = data.split(':')
            if int(group_id) != self.group:
                raise ValueError()
            owner, page = int(owner), int(page)
        except (TypeError, ValueError):
            answer(f'无效的页码，请重新发送 {page_command}。')
            return True
        msg = query.get('message', {})
        chat = msg.get('chat', {})
        if chat.get('type') != 'private' or chat.get('id') != owner or query['from']['id'] != owner:
            answer('请在自己的私聊中查询榜单。')
            return True
        try:
            member = self.call('getChatMember', chat_id=self.group, user_id=owner)
            active = member.get('status') in ('member', 'administrator', 'creator') or (
                member.get('status') == 'restricted' and member.get('is_member'))
            if not active:
                answer('请先加入群组后再查询榜单。')
                return True
            text, markup = (self.ranking_page if kind == 'rank' else self.gold_ranking)(owner, page)
            self.call('editMessageText', chat_id=owner, message_id=msg['message_id'],
                      text=text, parse_mode='HTML', reply_markup=markup)
        except APIError as exc:
            if 'message is not modified' not in str(exc).lower():
                answer('暂时无法刷新榜单，请稍后重试。')
                return True
        answer()
        return True


    def rate_command(self, msg):
        parts = msg.get('text', '').split()
        if not parts:
            return False
        cmd = parts[0].split('@', 1)
        if cmd[0].lower() != '/rate' or (len(cmd) > 1 and cmd[1].lower() != self.username.lower()):
            return False
        delay = None
        try:
            text = self.exchange.convert(parts[1:])
        except RateError as exc:
            text = str(exc)
            delay = self.notice_seconds()
        self.send(msg['chat']['id'], text, _delete_after=delay, reply_parameters={
            'message_id': msg['message_id'], 'allow_sending_without_reply': True})
        return True

    def bin_command(self, msg):
        parts = msg.get('text', '').split()
        if not parts:
            return False
        cmd = parts[0].split('@', 1)
        if cmd[0].lower() != '/bin' or (len(cmd)>1 and cmd[1].lower()!=self.username.lower()):
            return False
        chat = msg['chat']['id']
        delay = None
        if len(parts) != 2:
            result = '用法：/bin 45717360（输入 6–8 位 BIN）'
            delay = self.notice_seconds()
        else:
            try:
                result = format_result(parts[1], self.bin_lookup.lookup(parts[1]))
            except LookupError as exc:
                result = str(exc)
                delay = self.notice_seconds()
        self.send(chat, result, _delete_after=delay,
                  reply_parameters={'message_id':msg['message_id'], 'allow_sending_without_reply':True},
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
        everyone = ('<b>全员可用</b>\n'
                    '• /checkin — 私聊查询出金及保底；群内每日一次十连，逐抽计分后相加：三星 0、四星 1、五星常驻 5、五星 UP 10；单次最多 3 个四/五星；四星 10 抽、五星 90 抽保底跨天累计；出五星时群内播报（双黄/三黄）；要求头像对机器人可见，未公开会提示并计警告\n'
                    f'• 发言积分：每次 {self.message_points} 分，每天最多 {self.message_daily_limit} 分；头像不可见时静默不计分\n'
                    f'• /invite — 私聊领取专属邀请链接；成功邀请每人 1 分，每天最多 {INVITE_DAILY_LIMIT} 分；同一成员仅计一次\n'
                    '• /points — 私聊查看余额、今日积分与排名\n'
                    '• /report — 回复本群消息，提交 Jev 广告判断；1 名管理员或 5 名群员同意后警告一次\n'
                    '• /rank — 私聊查看排行榜，每页 50 人，按钮翻页，不艾特榜单成员\n'
                    '• 出金榜：/milestones — 出过五星（常驻或 UP）即上榜，按首次出金排序，不限人数，每页 50 人\n'
                    '• 汇率换算：/rate 100 USD CNY（或 /rate USD CNY）\n'
                    '• BIN 查询：/bin 45717360 — 输入 6–8 位 BIN\n'
                    f'• 入群验证：先关注关联频道，再完成算术题，{verify_time} 内最多答错 3 次\n'
                    '• 解封申请：被拉黑后私聊 /appeal 理由；也可先 /appeal 再填写，/cancel 取消填写；每天最多一次，待审不可重复提交')
        admins = ('<b>仅管理员</b>\n'
                  '• /manage [ID/@用户名] — 回复成员消息打开管理面板；查看警告、禁言、拉黑、解封和白名单，全部使用按钮；不填目标可查看群内机器人\n'
                  '• /manage — 查看本群黑白名单，选择成员管理\n'
                  '• /audit 最近 100 条全部日志；/audit ID 查看该用户日志；/status 运行状态；/help 查看此完整功能帮助')
        owner = ('<b>仅群主 · 私聊</b>\n'
                 '• /settings — 私聊选择群和配置项，直接发送新值，或 /cancel 取消\n'
                 '• /appeals — 查看含昵称提及、用户名和 ID 的待审申请，点击批准或拒绝；请先私聊 /start 以接收申请通知\n'
                 '• 批准后解除黑名单并清零警告，用户需重新申请入群；处理结果私聊通知申请人')
        automatic = (f'<b>自动规则</b>\n'
                     '• 签到出五星（常驻或 UP）时群内播报并进入出金榜，双黄/三黄合并播报\n'
                     f'• 达到 {self.upgrade_points} 分发送「第 N 位达标」恭喜消息，30 秒后自动删除；已有自定义标签也会通知\n'
                     f'• 符合条件时升级为「{html.escape(self.upgrade_tag)}」，不覆盖已有标签\n'
                     '• 连续 3 个完整自然日每天签到、没有正常发言：按本群默认禁言时长禁言，积分不清零\n'
                     '• 广告举报仅通过回复消息发送 /report 触发；群内投票确认后处罚\n'
                     '• 用户命令源消息保留；普通成功提示 30 秒后删除，失败及处罚通知 2 分钟；私聊旧填写步骤结束后 30 秒清理，预览和结果保留')
        footer = ('群内机器人操作不设每日次数上限；签到仍按规则执行。\n'
                  '管理目标可回复消息指定，或填写数字 ID / 已知 @用户名；匿名管理员请切换个人身份。')
        text = f'🤖 <b>功能菜单 · 2026-11-04</b>\n\n{everyone}\n\n{admins}\n\n{owner}\n\n{automatic}\n\n{footer}'
        self.send(chat, text, reply_parameters={'message_id': msg['message_id'], 'allow_sending_without_reply': True},
                  link_preview_options={'is_disabled': True},
                  )
        return True

    def command(self, msg):
        """Public moderation entry: /manage, /audit and /status are the only commands."""
        words = msg.get('text', '').split()
        if not words:
            return False
        command = words[0].split('@', 1)
        if len(command) > 1 and command[1].lower() != self.username.lower():
            return False
        name = command[0].lower()
        if name == '/manage':
            return self.moderation.command(msg)
        if name == '/audit':
            return self.audit_command(msg)
        if name == '/status':
            return self.status_command(msg)
        return False

    def admin_command(self, msg):
        """Anonymous senders and members without administrator rights get no action."""
        uid = msg.get('from', {}).get('id')
        if msg.get('sender_chat') or not uid:
            return False
        return self.admin(uid)

    def audit_command(self, msg):
        if not self.admin_command(msg):
            return True
        args = msg.get('text', '').split()[1:]
        if len(args) > 1:
            self.send(self.group, '用法：/audit 查看最近 100 条全部日志；/audit 用户ID或@用户名 查看该用户日志。',
                      _delete_after=self.notice_seconds())
            return True
        target_filter = None
        if args:
            try:
                if args[0].isdigit():
                    target_filter = int(args[0])
                    if target_filter <= 0:
                        raise ValueError()
                elif args[0].startswith('@'):
                    target_filter = self.members.resolve(args[0])
                    if not target_filter:
                        raise ValueError()
                else:
                    raise ValueError()
            except Exception:
                self.send(self.group, '无法确定目标用户，请填写有效数字 ID 或已知 @用户名。',
                          _delete_after=self.notice_seconds())
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
        if lines:
            chunks.append('\n'.join(lines))
        for page, chunk in enumerate(chunks, 1):
            self.send(self.group, f'{heading} · {page}/{len(chunks)}\n{chunk}')
        return True

    def status_command(self, msg):
        if not self.admin_command(msg):
            return True
        pending = self.db.execute("SELECT count(*) FROM verification WHERE kind!='approved'").fetchone()[0]
        banned = self.db.execute('SELECT count(*) FROM blacklist').fetchone()[0]
        family = self.db.execute("SELECT count(*) FROM upgrades WHERE tag=?", (self.upgrade_tag,)).fetchone()[0]
        tags = '正常' if self.can_manage_tags else '缺少 can_manage_tags 权限，已停用'
        white = self.db.execute('SELECT count(*) FROM whitelist').fetchone()[0]
        self.send(self.group, f'✅ 群管运行中\n待验证：{pending}\n黑名单：{banned}\n白名单：{white} 人\n标签升级：累计 {self.upgrade_points} 分改为「{self.upgrade_tag}」，已升级 {family} 人（{tags}）\n警告累计 3 次拉黑并删除消息。')
        return True

    def moderate(self, action, target, actor=0, event='', seconds=None, note=''):
        """统一成员管理动作服务：面板调用；公开命令不会进入这里。

        调用方须已完成群、操作者与目标校验；此处再拒绝非法目标和管理员。
        """
        if target <= 0 or target == self.bot_id or self.admin(target):
            self.send(self.group, '不能对管理员或本机器人执行此操作。',
                      _delete_after=self.notice_seconds())
            return False
        if action == 'warn':
            self.warn(target, event or f'manual:{actor}:{int(time.time())}',
                      note or '管理员手动警告', actor)
            return True
        if action == 'mute':
            duration = self.mute_seconds if seconds is None else int(seconds)
            if not 1 <= duration <= 31536000:
                self.send(self.group, '禁言时长无效，未执行。', _delete_after=self.notice_seconds())
                return False
            until = int(time.time()) + duration
            self.restrict(target, until)
            with self.db:
                self.db.execute('INSERT INTO warnings(uid,mute_until) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET mute_until=excluded.mute_until', (target, until))
            self.send(self.group, f'已禁言用户 {target}，时长 {duration // 60} 分钟。',
                      _delete_after=self.notice_seconds())
            self.audit(actor, target, 'mute', str(duration))
            return True
        if action == 'unmute':
            pending = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (target,)).fetchone()
            if pending:
                self.send(self.group, '该成员尚未完成入群验证，请先完成验证。',
                          _delete_after=self.notice_seconds())
                return False
            self.restore(target, clear_mute=True)
            with self.db:
                self.db.execute('UPDATE warnings SET mute_until=0 WHERE uid=?', (target,))
            self.send(self.group, f'已解除用户 {target} 的禁言。')
            self.audit(actor, target, 'unmute')
            return True
        if action == 'resetwarn':
            with self.db:
                self.db.execute('UPDATE warnings SET count=0 WHERE uid=?', (target,))
            self.send(self.group, f'已清除用户 {target} 的警告次数；现有禁言不受影响。')
            self.audit(actor, target, 'resetwarn')
            return True
        if action == 'ban':
            try:
                self.blacklist_member(target, actor, note or '管理员手动封禁')
            except APIError as exc:
                self.send(self.group, f'封禁用户 {target} 失败（{exc.code} {exc.description}），已保留黑名单记录。',
                          _delete_after=self.notice_seconds())
                return False
            self.send(self.group, f'🚫 已将用户 {target} 封禁并移出本群、加入黑名单，已请求 Telegram 删除其全部群消息。',
                      _delete_after=self.notice_seconds())
            return True
        if action == 'unban':
            if not self.blacklisted(target):
                self.send(self.group, f'用户 {target} 不在黑名单中。',
                          _delete_after=self.notice_seconds())
                return False
            self.unblacklist_member(target, actor)
            self.send(self.group, f'已解除用户 {target} 的黑名单并清零警告，可重新申请入群。')
            return True
        if action == 'white':
            if self.blacklisted(target):
                self.send(self.group, f'用户 {target} 在黑名单中，黑名单优先。如需加白名单请先在 /manage 面板解封。',
                          _delete_after=self.notice_seconds())
                return False
            note = (note or '')[:100]
            pending = self.db.execute("SELECT kind FROM verification WHERE uid=? AND kind!='approved'", (target,)).fetchone()
            with self.db:
                self.db.execute('INSERT INTO whitelist(uid,at,actor,note) VALUES(?,?,?,?) '
                                'ON CONFLICT(uid) DO UPDATE SET note=excluded.note,actor=excluded.actor',
                                (target, int(time.time()), actor, note))
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
            self.send(self.group, f'✅ 已将用户 {target} 加入白名单：豁免广告检测、三日异常签到禁言与入群验证。')
            self.audit(actor, target, 'whitelist', note)
            return True
        if action == 'unwhite':
            if not self.db.execute('SELECT 1 FROM whitelist WHERE uid=?', (target,)).fetchone():
                self.send(self.group, f'用户 {target} 不在白名单中。',
                          _delete_after=self.notice_seconds())
                return False
            with self.db:
                self.db.execute('DELETE FROM whitelist WHERE uid=?', (target,))
            self.send(self.group, f'已移除用户 {target} 的白名单，恢复正常限制。')
            self.audit(actor, target, 'unwhitelist')
            return True
        raise ValueError(f'未知管理动作：{action}')

    def reward_invite(self, user, inviter=None, timestamp=None):
        if user.get('is_bot') or user['id'] == self.bot_id or self.blacklisted(user['id']):
            return
        if inviter and (inviter.get('is_bot') or inviter['id'] == self.bot_id or
                        self.blacklisted(inviter['id']) or self.db.execute(
                            "SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (inviter['id'],)).fetchone()):
            inviter = None
        if inviter and inviter['id'] != user['id']:
            # Do not consume the update on a transient permission lookup failure.
            member = self.call('getChatMember', chat_id=self.group, user_id=inviter['id'])
            active = member.get('status') in ('creator', 'administrator', 'member') or (
                member.get('status') == 'restricted' and member.get('is_member'))
            if not active:
                inviter = None
        amount, status = self.points.invite(inviter, user['id'], timestamp)
        if status == 'added':
            self.upgrade_member(inviter['id'])

    def joined(self, user, inviter=None):
        if user.get('is_bot') and user['id']!=self.bot_id and not self.blacklisted(user['id']):
            self.members.remember(user)
            try:
                allowed=bool(inviter and not inviter.get('is_bot') and self.admin(inviter['id']))
            except APIError:
                allowed=False
            with self.db:
                self.db.execute('UPDATE known_bots SET inviter=?,approved=? WHERE uid=?',((inviter or {}).get('id'),int(allowed),user['id']))
            if allowed:
                with self.db:self.db.execute('DELETE FROM verification WHERE uid=?',(user['id'],))
                self.audit(inviter['id'],user['id'],'allow_invited_bot')
                return
            self.call('banChatMember',chat_id=self.group,user_id=user['id'])
            self.audit((inviter or {}).get('id',0),user['id'],'reject_invited_bot')
            return
        row = self.db.execute('SELECT * FROM verification WHERE uid=?', (user['id'],)).fetchone()
        if row and row['passed'] and row['expires'] > time.time():
            return
        self.start_verification(user, 'member', self.group)

    def handle(self, update):
        msg = update.get('message') or update.get('edited_message') or {}
        if not self.accepts_update(update):
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
            self.callback(update['callback_query'])
        elif 'chat_join_request' in update:
            req = update['chat_join_request']
            if req['chat']['id'] == self.group:
                if self.blacklisted(req['from']['id']):
                    self.call('declineChatJoinRequest', chat_id=self.group, user_id=req['from']['id'])
                    self.audit(0, req['from']['id'], 'blacklist_join_request')
                    return
                self.invitations.request(req)
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
                inviter = self.invitations.inviter(event)
                self.reward_invite(new['user'], inviter, event.get('date'))
                self.joined(new['user'],event.get('from'))
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
            if 'message' in update and self.invitations.command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.points_command(msg):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and (self.rate_command(msg) or self.bin_command(msg)):
                return
            if msg['chat'].get('type') == 'private' and 'message' in update and self.command(msg):
                return
            if msg['chat']['id'] != self.group:
                if msg['chat'].get('type') == 'private':
                    self.send(msg['chat']['id'], '这里只处理机器人支持的私聊功能，发送 /help 查看可用功能。')
                return
        # sender_chat identifies channel posts even when Telegram also includes a bot-like from field.
            sender = msg.get('sender_chat')
            if not sender and msg.get('from', {}).get('is_bot') and msg.get('message_id'):
                # Telegram only exposes some other-bot messages; queue the ones we do see.
                self.schedule_delete(msg['chat']['id'], msg['message_id'])
            for user in msg.get('new_chat_members', []):
                self.joined(user,msg.get('from'))
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
            if not msg['from'].get('is_bot'):
                with self.db:
                    self.db.execute('INSERT INTO user_activity(uid,last_active) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET last_active=excluded.last_active', (uid, int(msg.get('date') or time.time())))
            if 'message' in update and not self.whitelisted(uid):
                self.attention.observed(msg)
            if 'message' in update and self.invitations.command(msg):
                return
            if 'message' in update and self.help_command(msg):
                return
            if 'message' in update and self.points_command(msg):
                return
            if 'message' in update and (self.rate_command(msg) or self.bin_command(msg)):
                return
            if 'message' in update and self.command(msg):
                return
            uid = msg['from']['id']
            if uid == self.bot_id or msg['from'].get('is_bot'):
                return
            pending = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (uid,)).fetchone()
            if pending and not self.whitelisted(uid):
                return
            content = any(msg.get(key) for key in ('text','photo','video','animation','audio','voice','video_note','document','sticker','poll','contact','location','venue','dice'))
            if 'message' in update and content and not msg.get('text','').startswith('/') and self.avatar_visible(uid, message_id=msg.get('message_id'), purpose='message') is True:
                amount, status = self.points.award(msg['from'],'message',f'message:{self.group}:{msg["message_id"]}',msg.get('date'))
                if status == 'added':
                    self.upgrade_member(uid)

    def expire(self):
        rows = self.db.execute("SELECT * FROM verification WHERE expires<? AND kind!='approved'", (int(time.time()),)).fetchall()
        for row in rows:
            if self.whitelisted(row['uid']):
                try:
                    if row['kind'] == 'member':
                        self.restore(row['uid'])
                    elif row['kind'] == 'request':
                        self.call('approveChatJoinRequest', chat_id=self.group, user_id=row['uid'])
                except APIError as exc:
                    if row['kind'] != 'request' or exc.code != 400 or 'user_already_participant' not in exc.description.lower():
                        LOG.warning('Whitelist verification restore failed uid=%s: %s', row['uid'], exc)
                        continue
                with self.db:
                    self.db.execute("UPDATE verification SET passed=1,kind='approved',expires=? WHERE uid=?",
                                    (int(time.time()) + 86400, row['uid']))
                continue
            try:
                self.reject(row)
            except APIError as exc:
                LOG.warning('Expiry failed uid=%s: %s', row['uid'], exc)

    def _settings_owner_status(self, bot, uid):
        try:
            return bot.call('getChatMember', chat_id=bot.group, user_id=uid).get('status') == 'creator'
        except APIError:
            return None

    def _setting_display(self, key, value):
        if self.GROUP_SETTING_SPECS[key]['type'] == 'bool':
            return '开启' if value else '关闭'
        return str(value) if value != '' else '未设置'

    def _settings_panel(self, bot, title, uid):
        channel_id = self.channel
        channel_url = self.channel_url
        lines = [f'⚙️ <b>{html.escape(title)}</b>（<code>{bot.group}</code>）设置',
                 '点击下方项目修改；数值设置请直接发送新值，或用 /cancel 取消。']
        for key, spec in bot.GROUP_SETTING_SPECS.items():
            value = html.escape(self._setting_display(key, bot.setting_values[key]))
            origin = '自定义' if key in bot.setting_overrides else '默认'
            lines.append(f'• {html.escape(spec["label"])}：<b>{value}</b> · {origin}')
        escaped_url = html.escape(channel_url, quote=True)
        channel_text = f'<a href="{escaped_url}">{html.escape(channel_url)}</a>' if channel_url else '未设置'
        lines.extend([f'• 入群验证频道：<code>{channel_id}</code>',
                      f'• 关注频道链接：{channel_text}',
                      '', '机器人令牌、AI API 密钥和 API 地址由部署环境管理。'])

        values = [(key, spec['label'], self._setting_display(key, bot.setting_values[key]))
                  for key, spec in bot.GROUP_SETTING_SPECS.items()]
        rows = []
        pair = []
        for key, label, value in values:
            spec = bot.GROUP_SETTING_SPECS[key]
            action = 't' if spec['type'] == 'bool' else 'e'
            value_label = '开' if spec['type'] == 'bool' and bot.setting_values[key] else (
                '关' if spec['type'] == 'bool' else str(value))
            button = {'text': f'{label} · {value_label}'[:60],
                      'callback_data': f'gs:{action}:{bot.group}:{uid}:{key}'}
            pair.append(button)
            if len(pair) == 2:
                rows.append(pair)
                pair = []
        if pair:
            rows.append(pair)
        rows.append([
            {'text': '入群验证频道', 'callback_data': f'gs:e:{bot.group}:{uid}:CHANNEL_ID'},
            {'text': '频道邀请链接', 'callback_data': f'gs:e:{bot.group}:{uid}:CHANNEL_URL'},
        ])
        resettable = []
        for key in bot.GROUP_SETTING_SPECS:
            if key not in bot.setting_overrides:
                continue
            resettable.append({'text': f'恢复{bot.GROUP_SETTING_SPECS[key]["label"][:14]}',
                               'callback_data': f'gs:r:{bot.group}:{uid}:{key}'})
        for index in range(0, len(resettable), 2):
            rows.append(resettable[index:index + 2])
        return '\n'.join(lines), {'inline_keyboard': rows}

    def _settings_title(self, gid):
        return self.group_title

    def _show_settings_panel(self, uid, bot, message=None):
        text, markup = self._settings_panel(bot, self._settings_title(bot.group), uid)
        if message:
            self.call('editMessageText', chat_id=message['chat']['id'],
                      message_id=message['message_id'], text=text, parse_mode='HTML',
                      reply_markup=markup, disable_web_page_preview=True)
        else:
            self.send(uid, text, keep=True, reply_markup=markup, disable_web_page_preview=True)

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
            self.send(chat['id'], '设置现已改为按钮操作，请直接发送 /settings 。',
                      _delete_after=self.notice_seconds())
            return True
        if self._settings_owner_status(self, uid):
            self._show_settings_panel(uid, self)
        else:
            self.send(chat['id'], '只有本群群主可以查看和修改设置。', _delete_after=0)
        return True

    def _settings_value_prompt(self, uid, bot, key, panel_message, error=''):
        prompts = {
            'CHANNEL_ID': '请直接发送该群绑定的频道 @用户名 或频道数字 ID。机器人必须是该频道管理员。',
            'CHANNEL_URL': '请直接发送关注频道链接（https://t.me/...），支持公开频道和私有邀请链接。',
        }
        spec = bot.GROUP_SETTING_SPECS.get(key)
        prompt = prompts.get(key)
        if not prompt and spec:
            prompt = f'请直接发送“{spec["label"]}”的新值。'
            if spec['type'] == 'int':
                prompt += f' 范围：{spec["min"]}–{spec["max"]}。'
            elif spec['type'] == 'tag':
                prompt += ' 限 1–16 个 UTF-16 字符，不能含 Emoji。'
        response = self.send(uid, (html.escape(error) + '\n' if error else '') + prompt + '\n直接发送新值，或用 /cancel 取消（10 分钟内有效）。',
                             keep=True)
        self.track_private_step(uid, response['message_id'], 'settings', lifetime=600)
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO group_setting_sessions'
                            '(uid,group_id,key,prompt_message_id,panel_message_id,expires) VALUES(?,?,?,?,?,?)',
                            (uid, bot.group, key, response['message_id'], panel_message['message_id'],
                             int(time.time()) + 600))

    def _valid_channel_url(self, raw):
        value = raw.strip()
        parsed = urllib.parse.urlsplit(value)
        if (parsed.scheme != 'https' or parsed.hostname not in ('t.me', 'telegram.me') or
                parsed.username or parsed.password or parsed.port or not parsed.path.strip('/') or
                parsed.query or parsed.fragment or any(char.isspace() for char in value)):
            raise ValueError('请提供有效的 https://t.me/... 频道链接。')
        return 'https://' + parsed.netloc.lower() + parsed.path

    def _save_setting(self, bot, key, value):
        with bot.db:
            bot.db.execute('INSERT OR REPLACE INTO group_settings(key,value) VALUES(?,?)',
                              (key, '1' if value is True else '0' if value is False else str(value)))
        bot.setting_values[key] = value
        bot.setting_overrides.add(key)
        bot._apply_group_settings()

    def _reset_setting(self, bot, key):
        keys = (key,)
        with bot.db:
            bot.db.executemany('DELETE FROM group_settings WHERE key=?', ((item,) for item in keys))
        for item in keys:
            bot.setting_values[item] = bot.setting_defaults[item]
            bot.setting_overrides.discard(item)
        bot._apply_group_settings()

    def _settings_refresh_message(self, uid, bot, message):
        try:
            self._show_settings_panel(uid, bot, message)
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
            if action in ('e', 't', 'r') and len(fields) == 5:
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
        bot = self
        if gid != self.group:
            answer('该管理群已不存在。')
            return True
        status = self._settings_owner_status(bot, uid)
        if status is None:
            answer('暂时无法核实你的群主身份。')
            return True
        if not status:
            answer('只有该群群主可以修改设置。')
            return True
        if action == 't':
            if key not in bot.GROUP_SETTING_SPECS or bot.GROUP_SETTING_SPECS[key]['type'] != 'bool':
                answer('设置按钮无效。')
                return True
            value = not bot.setting_values[key]
            self._save_setting(bot, key, value)
            bot.audit(uid, 0, 'group_setting_set', f'{key}={value}')
            self._settings_refresh_message(uid, bot, msg)
            answer()
            return True
        if action == 'r':
            if key in bot.GROUP_SETTING_SPECS and key in bot.setting_overrides:
                self._reset_setting(bot, key)
            else:
                answer('此项目没有自定义值可恢复。')
                return True
            bot.audit(uid, 0, 'group_setting_reset', key)
            self._settings_refresh_message(uid, bot, msg)
            answer()
            return True
        if action == 'e':
            allowed = set(bot.GROUP_SETTING_SPECS) - {'JEV_ENABLED'}
            allowed.update(('CHANNEL_ID', 'CHANNEL_URL'))
            if key not in allowed:
                answer('设置按钮无效。')
                return True
            self._settings_value_prompt(uid, bot, key, msg)
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
            self.finish_private_steps(uid, 'settings')
            self.send(uid, '已取消本次设置。')
            return True
        if int(session['expires']) < time.time():
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.finish_private_steps(uid, 'settings')
            return False
        bot = self
        if session['group_id'] != self.group or self._settings_owner_status(bot, uid) is not True:
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.finish_private_steps(uid, 'settings')
            self.send(uid, '群主身份已变化，设置操作已取消。')
            return True
        key = session['key']
        if key not in bot.GROUP_SETTING_SPECS and key not in ('CHANNEL_ID', 'CHANNEL_URL'):
            with self.db:
                self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.finish_private_steps(uid, 'settings')
            self.send(uid, '此设置已移除，请重新发送 /settings。')
            return True
        if text.startswith('/') or not text:
            self.send(uid, '请直接发送新参数，或用 /cancel 取消。')
            return True
        try:
            if key == 'CHANNEL_ID':
                info = self.call('getChat', chat_id=text)
                if info.get('type') != 'channel':
                    raise ValueError('该 ID/用户名不是频道。')
                membership = self.call('getChatMember', chat_id=info['id'], user_id=self.bot_id)
                if membership.get('status') not in ('administrator', 'creator'):
                    raise ValueError('机器人必须先成为该频道管理员。')
                link = self._channel_url_from_chat(info, '')
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES('verification_channel',?)", (str(info['id']),))
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES('verification_channel_url',?)", (link,))
                bot.channel = int(info['id'])
                bot.channel_url = link
                bot.cfg['CHANNEL_ID'] = str(info['id'])
                bot.cfg['TELEGRAM_CHANNEL_URL'] = link
                audit_value = str(info['id'])
            elif key == 'CHANNEL_URL':
                value = self._valid_channel_url(text)
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO meta VALUES('verification_channel_url',?)", (value,))
                bot.channel_url = value
                bot.cfg['TELEGRAM_CHANNEL_URL'] = value
                audit_value = value
            else:
                value = bot.parse_group_setting(key, text)
                self._save_setting(bot, key, value)
                audit_value = str(value)
        except (ValueError, APIError, KeyError) as exc:
            error = str(exc) if isinstance(exc, ValueError) else '读取或验证频道失败，请检查频道 ID 和机器人权限后重试。'
            self._settings_value_prompt(uid, bot, key,
                                        {'message_id': session['panel_message_id']}, error)
            return True
        with self.db:
            self.db.execute('DELETE FROM group_setting_sessions WHERE uid=?', (uid,))
            self.finish_private_steps(uid, 'settings')
        bot.audit(uid, 0, 'group_setting_set', f'{key}={audit_value}')
        panel_msg = {'chat': msg['chat'], 'message_id': session['panel_message_id']}
        self._settings_refresh_message(uid, bot, panel_msg)
        return True

    def _validate_permissions(self):
        bot = self
        channel_member=bot.call('getChatMember',chat_id=bot.channel,user_id=self.bot_id)
        if channel_member.get('status') not in ('administrator','creator'):
            raise RuntimeError(f'Bot must be administrator in channel {bot.channel}')
        member = bot.call('getChatMember', chat_id=bot.group, user_id=self.bot_id)
        if member.get('status') not in ('administrator','creator'):
            raise RuntimeError(f'Bot must be administrator in group {bot.group}')
        if member.get('status') != 'creator':
            for right in ('can_delete_messages','can_restrict_members','can_invite_users'):
                if not member.get(right): raise RuntimeError(f'Missing administrator right {right} in group {bot.group}')
        bot.can_manage_tags = bool(member.get('status') == 'creator' or member.get('can_manage_tags'))

    def accepts_update(self, update):
        payload = update.get('message') or update.get('edited_message') or update.get('chat_member') or update.get('chat_join_request') or update.get('my_chat_member')
        if payload:
            chat = payload.get('chat', {})
            return chat.get('type') == 'private' or chat.get('id') == self.group
        query = update.get('callback_query')
        if query:
            chat = query.get('message', {}).get('chat', {})
            return chat.get('id') == self.group or (chat.get('type') == 'private' and chat.get('id') == query.get('from', {}).get('id'))
        return False

    def maintenance(self):
        if not hasattr(self, '_maintenance_due'):
            self._maintenance_due = {}
        tasks = [('verification', self.expire),
                 ('deletions', self.sweep_deletions),
                 ('jev', self.jev.work),
                 ('attention', self.attention.tick), ('appeals', self.appeals.tick),
                 ('milestones', self.milestones.tick)]
        for name, fn in tasks:
            if self._maintenance_due.get(name, 0) > time.time():
                continue
            try:
                fn()
            except Exception as exc:
                self._maintenance_due[name] = time.time() + max(30, getattr(exc, 'retry_after', 0))
                LOG.warning('Maintenance deferred task=%s type=%s', name, type(exc).__name__)
            else:
                self._maintenance_due.pop(name, None)

    def run(self):
        me = self.call('getMe')
        self.username, self.bot_id = me['username'], me['id']
        if self.call('getWebhookInfo').get('url'):
            raise RuntimeError('Existing webhook configured; refusing to replace it')
        self.group_title = self.call('getChat', chat_id=self.group).get('title') or self.group_title
        self._validate_permissions()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('start_time',?)", (str(int(time.time())),))
        start_time = int(self.db.execute("SELECT value FROM meta WHERE key='start_time'").fetchone()[0])
        row = self.db.execute("SELECT value FROM meta WHERE key='offset'").fetchone()
        offset = int(row[0]) if row else 0
        LOG.info('Started @%s group=%s', self.username, self.group)
        while True:
            try:
                self.maintenance()
                quick = self.jev.busy() or self.pending_deletions()
                updates = self.call('getUpdates', offset=offset, timeout=1 if quick else 20,
                                    allowed_updates=['message', 'edited_message', 'chat_member', 'chat_join_request', 'callback_query', 'my_chat_member'])
                for update in updates:
                    payload = next((update[k] for k in ('edited_message','message','chat_member','chat_join_request','my_chat_member') if k in update), {})
                    event_time = payload.get('edit_date', payload.get('date', start_time))
                    if payload.get('chat', {}).get('type') == 'private':
                        entry = (payload.get('text', '').split()[:1] or [''])[0].split('@')[0].lower()
                        LOG.info('Private update=%s uid=%s entry=%s', update['update_id'], payload.get('from', {}).get('id'), entry if entry in ('/start','/help','/cancel') else 'other')
                    if self.accepts_update(update) and event_time >= start_time and not self.db.execute('SELECT 1 FROM processed WHERE id=?', (update['update_id'],)).fetchone():
                        try:
                            if 'callback_query' in update and update['callback_query'].get('data','').startswith('gs:'):
                                self.group_settings_callback(update['callback_query'])
                            elif 'message' in update and self.group_settings_reply(update['message']):
                                pass
                            elif ((update.get('message') or {}).get('chat',{}).get('type')=='private'
                                  and ((update.get('message') or {}).get('text','').split()[:1] or [''])[0].split('@')[0].lower()
                                  == '/settings'):
                                self.group_settings_command(update['message'])
                            else:
                                self.handle(update)
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
            except APIError as exc:
                LOG.warning('%s', exc)
                time.sleep(min(max(exc.retry_after, 3), 60))
            except Exception:
                LOG.exception('Update failed; will retry')
                time.sleep(5)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    Bot(config()).run()
