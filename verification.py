# coding: utf-8
"""入群验证：先关注关联频道，再答对算术题；超时或连续答错即拒绝。

状态保存在 verification 表：``state`` 为 ``challenge``（等待作答）或 ``passed``
（已通过，保留 PASSED_TTL 秒，避免批准入群时再次触发验证）。同一成员只保留一行，
重复的入群/申请更新沿用同一道题，不会重复发卡片。
"""
import json
import logging
import secrets
import time

from botapi import APIError

LOG = logging.getLogger('groupbot.verification')

MAX_ATTEMPTS = 3
PASSED_TTL = 86400
DELIVERY_ATTEMPTS = 3
DELIVERY_RETRY_SECONDS = 30
FINISH_RETRY_SECONDS = 30
RESTORE_ATTEMPTS = 5
APPROVE_ATTEMPTS = 5
FINISH_ATTEMPTS = 10

# 这些描述说明对象或申请已经不存在，重试没有意义。
SETTLED_ERRORS = ('user is deactivated', 'user_id_invalid', 'hide_requester_missing',
                  'user_already_participant', 'participant_id_invalid', 'user_not_participant',
                  'chat not found')


class Verification:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS verification(
          uid INTEGER PRIMARY KEY,
          chat INTEGER NOT NULL,
          kind TEXT NOT NULL,
          nonce TEXT NOT NULL,
          question TEXT NOT NULL,
          answer INTEGER NOT NULL,
          options TEXT NOT NULL,
          expires INTEGER NOT NULL,
          attempts INTEGER NOT NULL DEFAULT 0,
          state TEXT NOT NULL DEFAULT 'challenge',
          sent INTEGER NOT NULL DEFAULT 0,
          send_attempts INTEGER NOT NULL DEFAULT 0,
          restored INTEGER NOT NULL DEFAULT 0,
          restore_attempts INTEGER NOT NULL DEFAULT 0,
          approved INTEGER NOT NULL DEFAULT 0,
          approve_attempts INTEGER NOT NULL DEFAULT 0,
          finish_attempts INTEGER NOT NULL DEFAULT 0,
          retry_at INTEGER NOT NULL DEFAULT 0,
          updated INTEGER NOT NULL DEFAULT 0);
        ''')
        self.require_schema()

    REQUIRED_COLUMNS = frozenset({
        'uid', 'chat', 'kind', 'nonce', 'question', 'answer', 'options', 'expires',
        'attempts', 'state', 'sent', 'send_attempts', 'restored', 'restore_attempts',
        'approved', 'approve_attempts', 'finish_attempts', 'retry_at', 'updated'})

    def require_schema(self):
        """不接受其他版本的验证表：没有迁移分支，结构不符时直接拒绝启动。"""
        columns = {row['name'] for row in self.db.execute('PRAGMA table_info(verification)')}
        missing = self.REQUIRED_COLUMNS - columns
        if missing:
            raise RuntimeError(
                'verification table has an incompatible schema; missing columns: '
                + ', '.join(sorted(missing))
                + '. Drop verification, then restart.')

    # --- 入口 -------------------------------------------------------------

    def admit(self, user, kind, chat, inviter=None):
        """处理一次入群或入群申请；kind 为 ``request`` 或 ``member``。"""
        user = user or {}
        uid = int(user.get('id') or 0)
        if not uid or uid == self.bot.bot_id:
            return
        if user.get('is_bot'):
            if inviter and not inviter.get('is_bot') and self._is_admin(int(inviter.get('id') or 0)):
                self.bot.audit(int(inviter['id']), uid, 'allow_invited_bot')
                return
            self._reject(uid, kind, 'reject_bot')
            return
        if self._is_admin(uid):
            return
        now = int(time.time())
        row = self.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()
        if row and row['state'] == 'passed' and row['expires'] > now:
            # 已验证过的成员重新申请入群，不再重复验证。
            if kind == 'request':
                self._approve(row)
            return
        if row and row['state'] == 'challenge' and row['expires'] > now and row['kind'] == kind:
            if not row['sent'] and row['retry_at'] <= now:
                self._deliver(row)
            return
        self._create(user, kind, chat, now)

    def forget(self, uid):
        """成员离开群时取消尚未完成的验证；已通过的记录保留到过期。"""
        with self.db:
            self.db.execute("DELETE FROM verification WHERE uid=? AND state='challenge'", (uid,))

    def pending(self, uid):
        row = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND state='challenge'", (uid,)).fetchone()
        return bool(row)

    # --- 按钮作答 ---------------------------------------------------------

    def callback(self, query):
        """处理 ``v:{用户}:{随机串}:{答案}`` 按钮；不属于验证的回调返回 False。"""
        data = (query.get('data') or '').split(':')
        if len(data) != 4 or data[0] != 'v':
            return False
        try:
            uid, answer = int(data[1]), int(data[3])
        except ValueError:
            return False
        sender = (query.get('from') or {}).get('id')
        if uid != sender:
            self._answer(query, '这是其他成员的验证，请完成你自己的验证。')
            return True
        row = self.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()
        if not row or row['state'] != 'challenge' or row['nonce'] != data[2] or row['expires'] < time.time():
            self._answer(query, '这条验证已失效，请重新申请入群以获取新的题目。')
            return True
        try:
            subscribed = self.bot.subscribed(uid)
        except APIError as exc:
            LOG.warning('Channel check failed uid=%s code=%s', uid, exc.code)
            self._answer(query, '暂时无法确认频道关注状态，请稍后重试。')
            return True
        if not subscribed:
            # 未关注频道不消耗答错次数，也不判定答案。
            self._answer(query, '尚未检测到关注频道。请关注后再次点击正确答案。')
            return True
        if answer != row['answer']:
            self._wrong(row, query)
            return True
        self._pass(row, query)
        return True

    def _wrong(self, row, query):
        uid = row['uid']
        attempts = row['attempts'] + 1
        with self.db:
            self.db.execute('UPDATE verification SET attempts=? WHERE uid=?', (attempts, uid))
        if attempts >= MAX_ATTEMPTS:
            self._fail(dict(row, attempts=attempts), 'attempts')
            self._answer(query, f'答错已达 {MAX_ATTEMPTS} 次，验证未通过，请重新申请入群。')
            return
        self._answer(query, f'答案不正确，还剩 {MAX_ATTEMPTS - attempts} 次机会。')

    def _pass(self, row, query):
        uid = row['uid']
        now = int(time.time())
        # 先落库通过状态：随后的入群更新不能再触发一次验证。
        with self.db:
            self.db.execute("UPDATE verification SET state='passed', expires=?, updated=? WHERE uid=?",
                            (now + PASSED_TTL, now, uid))
        if row['kind'] == 'request':
            outcome = self._approve(row)
            if outcome is None:
                # 申请已被撤销、过期或用户已不存在：记录已清理，可以重新申请。
                self._answer(query, '验证已通过，但入群申请已失效，请重新申请入群。')
                return
            self.bot.audit(0, uid, 'verified')
            if outcome:
                self._answer(query, '验证成功，欢迎入群！')
                self._edit_card(query, '✅ 已通过频道关注与人机验证。')
            else:
                # 验证已通过，但批准入群暂时失败：不谎报成功，后台继续重试。
                self._answer(query, '验证已通过，正在为你提交入群申请，请稍候。')
                self._edit_card(query, '✅ 已通过验证，正在提交入群申请。')
            return
        self.bot.audit(0, uid, 'verified')
        if self._restore(uid):
            self._answer(query, '验证成功，欢迎入群！')
            self._edit_card(query, '✅ 已通过频道关注与人机验证。')
        else:
            # 验证本身通过，但发言权限没恢复：不谎报“成功”，交给后台重试。
            self._answer(query, '验证已通过，但恢复发言失败；机器人会自动重试，仍无法发言请联系管理员。')
            self._edit_card(query, '✅ 已通过验证，恢复发言失败，正在自动重试。')

    # --- 后台维护 ---------------------------------------------------------

    def tick(self):
        now = int(time.time())
        for row in self.db.execute(
                "SELECT * FROM verification WHERE state='challenge' AND expires<? "
                "AND finish_attempts<? AND (retry_at=0 OR retry_at<=?)",
                (now, FINISH_ATTEMPTS, now)).fetchall():
            self._fail(row, 'timeout')
        for row in self.db.execute(
                "SELECT * FROM verification WHERE state='challenge' AND sent=0 AND expires>=? "
                "AND retry_at<=?", (now, now)).fetchall():
            if row['send_attempts'] >= DELIVERY_ATTEMPTS:
                self._fail(row, 'delivery_failed')
            else:
                self._deliver(row)
        for row in self.db.execute(
                "SELECT * FROM verification WHERE state='passed' AND kind='request' AND approved=0 "
                "AND approve_attempts<? AND retry_at<=?",
                (APPROVE_ATTEMPTS, now)).fetchall():
            self._approve(row)
        for row in self.db.execute(
                "SELECT uid,restore_attempts FROM verification WHERE state='passed' AND restored=0 "
                "AND restore_attempts<? AND retry_at<=?",
                (RESTORE_ATTEMPTS, now)).fetchall():
            self._restore(row['uid'], row['restore_attempts'])
        with self.db:
            self.db.execute("DELETE FROM verification WHERE state='passed' AND expires<?", (now,))

    # --- 内部步骤 ---------------------------------------------------------

    def _create(self, user, kind, chat, now):
        uid = int(user['id'])
        a, b = secrets.randbelow(8) + 2, secrets.randbelow(8) + 2
        answer = a + b
        options = [answer, answer + 1, answer + 2, answer - 1]
        secrets.SystemRandom().shuffle(options)
        # 先落库再禁言：即使禁言或投递失败，也存在可追踪、可重试的记录。
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO verification(uid,chat,kind,nonce,question,answer,options,expires,"
                "attempts,state,sent,send_attempts,restored,restore_attempts,approved,approve_attempts,"
                "finish_attempts,retry_at,updated) "
                "VALUES(?,?,?,?,?,?,?,?,0,'challenge',0,0,0,0,0,0,0,0,?)",
                (uid, chat, kind, secrets.token_hex(6), f'{a} + {b}', answer,
                 json.dumps(options), now + self.bot.verify_seconds, now))
        row = self.db.execute('SELECT * FROM verification WHERE uid=?', (uid,)).fetchone()
        if kind == 'member':
            try:
                # 直接进群：先禁言，验证通过后再恢复发言。
                self.bot.restrict(uid)
            except APIError as exc:
                LOG.warning('Cannot restrict joining member uid=%s code=%s', uid, exc.code)
                self.bot.audit(0, uid, 'verification_restrict_failed')
                # 无法禁言就不能让未验证成员留在群里发言。
                self._fail(row, 'restrict_failed')
                return
        self._deliver(row)

    def _deliver(self, row):
        uid = row['uid']
        try:
            # 卡片发出后一直保留在聊天里，结束时只编辑文案，不安排删除。
            self.bot.send(row['chat'], self._card_text(row), reply_markup=self._keyboard(row))
        except APIError as exc:
            attempts = row['send_attempts'] + 1
            LOG.warning('Verification delivery failed uid=%s code=%s attempt=%s', uid, exc.code, attempts)
            if exc.permanent or attempts >= DELIVERY_ATTEMPTS:
                self._fail(dict(row, send_attempts=attempts), 'delivery_failed')
                return
            with self.db:
                self.db.execute('UPDATE verification SET send_attempts=?, retry_at=? WHERE uid=?',
                                (attempts, int(time.time()) + DELIVERY_RETRY_SECONDS, uid))
            return
        with self.db:
            self.db.execute('UPDATE verification SET sent=1, send_attempts=send_attempts+1, retry_at=0 '
                            'WHERE uid=?', (uid,))

    def _fail(self, row, reason):
        """结束一个未通过的验证：拒绝申请或移出成员；失败时标记过期并重试。"""
        uid = row['uid']
        with self.db:
            # 先标记失效：即使随后的 Telegram 调用失败，后台也会按超时继续收尾。
            self.db.execute('UPDATE verification SET expires=0, updated=? WHERE uid=?',
                            (int(time.time()), uid))
        method = 'declineChatJoinRequest' if row['kind'] == 'request' else 'unbanChatMember'
        payload = {'chat_id': self.bot.group, 'user_id': uid}
        if method == 'unbanChatMember':
            payload['only_if_banned'] = False
        try:
            self.bot.call(method, **payload)
        except APIError as exc:
            if not (exc.permanent and self._settled(exc)):
                attempts = row['finish_attempts'] + 1
                LOG.warning('Verification conclusion deferred uid=%s reason=%s code=%s attempts=%s',
                            uid, reason, exc.code, attempts)
                with self.db:
                    self.db.execute('UPDATE verification SET finish_attempts=?, retry_at=? WHERE uid=?',
                                    (attempts, int(time.time()) + FINISH_RETRY_SECONDS, uid))
                if attempts >= FINISH_ATTEMPTS:
                    # 持续失败不再重试，保留记录并写审计，等待人工处理。
                    self.bot.audit(0, uid, 'verification_conclusion_gaveup:' + reason)
                return
        with self.db:
            self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
        self.bot.audit(0, uid, f'verification_{reason}')

    def _restore(self, uid, attempts=0):
        try:
            self.bot.restore(uid)
        except APIError as exc:
            attempts += 1
            with self.db:
                self.db.execute('UPDATE verification SET restore_attempts=?, retry_at=? WHERE uid=?',
                                (attempts, int(time.time()) + FINISH_RETRY_SECONDS, uid))
            LOG.warning('Restore permissions failed uid=%s code=%s attempts=%s',
                        uid, exc.code, attempts)
            if attempts >= RESTORE_ATTEMPTS:
                # 达到上限后不再重试：成员仍被限制，写审计并在群里请管理员接手。
                self.bot.audit(0, uid, 'verification_restore_gaveup')
                self._notify_admin(uid)
            return False
        with self.db:
            self.db.execute('UPDATE verification SET restored=1, retry_at=0 WHERE uid=?', (uid,))
        return True

    def _notify_admin(self, uid):
        """恢复发言持续失败时提醒管理员，避免成员被静默禁言。"""
        try:
            self.bot.send(self.bot.group,
                          f'⚠️ 成员 <a href="tg://user?id={uid}">UID {uid}</a> 已通过验证，'
                          '但机器人多次恢复发言失败，请在群设置中手动解除限制。')
        except APIError as exc:
            LOG.warning('Admin notice failed uid=%s code=%s', uid, exc.code)

    def _reject(self, uid, kind, reason):
        method = 'declineChatJoinRequest' if kind == 'request' else 'banChatMember'
        try:
            self.bot.call(method, chat_id=self.bot.group, user_id=uid)
        except APIError as exc:
            if not (exc.permanent and self._settled(exc)):
                LOG.warning('Reject failed uid=%s reason=%s code=%s', uid, reason, exc.code)
                self.bot.audit(0, uid, 'reject_failed:' + reason)
                return
        self.bot.audit(0, uid, reason)

    def _approve(self, row):
        """批准入群：True=已批准，False=稍后重试，None=申请已失效。"""
        uid = row['uid']
        try:
            self.bot.call('approveChatJoinRequest', chat_id=self.bot.group, user_id=uid)
        except APIError as exc:
            description = exc.description.lower()
            if exc.permanent and 'user_already_participant' in description:
                pass
            elif exc.permanent and self._settled(exc):
                # 申请已撤销/过期/用户已注销：清掉记录，允许重新申请。
                with self.db:
                    self.db.execute('DELETE FROM verification WHERE uid=?', (uid,))
                self.bot.audit(0, uid, 'verification_approve_expired')
                return None
            else:
                attempts = row['approve_attempts'] + 1
                LOG.warning('Approve join request failed uid=%s code=%s attempts=%s',
                            uid, exc.code, attempts)
                with self.db:
                    self.db.execute('UPDATE verification SET approve_attempts=?, retry_at=? WHERE uid=?',
                                    (attempts, int(time.time()) + FINISH_RETRY_SECONDS, uid))
                if attempts >= APPROVE_ATTEMPTS:
                    # 申请仍在 Telegram 侧挂起，需要人工批准或撤销。
                    self.bot.audit(0, uid, 'verification_approve_gaveup')
                return False
        with self.db:
            self.db.execute('UPDATE verification SET approved=1, retry_at=0 WHERE uid=?', (uid,))
        return True

    def _is_admin(self, uid):
        if uid <= 0:
            return False
        try:
            return bool(self.bot.admin(uid))
        except APIError:
            return None

    @staticmethod
    def _settled(exc):
        description = exc.description.lower()
        return any(term in description for term in SETTLED_ERRORS)

    def _card_text(self, row):
        return (f'<a href="tg://user?id={row["uid"]}">新成员</a>，请先关注频道，'
                f'再选择 {row["question"]} 的答案。\n'
                f'验证限时 {self.limit_text()}，最多答错 {MAX_ATTEMPTS} 次，仅限本人点击。')

    def limit_text(self):
        seconds = self.bot.verify_seconds
        if seconds < 60:
            return f'{seconds} 秒'
        minutes, rest = divmod(seconds, 60)
        return f'{minutes} 分钟' if not rest else f'{minutes} 分 {rest} 秒'

    def _keyboard(self, row):
        options = json.loads(row['options'])
        rows = []
        if self.bot.channel_url:
            rows.append([{'text': '① 关注频道', 'url': self.bot.channel_url}])
        rows.append([{'text': str(number), 'callback_data': f'v:{row["uid"]}:{row["nonce"]}:{number}'}
                     for number in options])
        return {'inline_keyboard': rows}

    def _answer(self, query, text):
        callback_id = query.get('id')
        if not callback_id:
            return
        try:
            self.bot.call('answerCallbackQuery', callback_query_id=callback_id, text=text, show_alert=True)
        except APIError as exc:
            LOG.info('Callback acknowledgement failed code=%s', exc.code)

    def _edit_card(self, query, text):
        message = query.get('message') or {}
        if not message.get('message_id') or not message.get('chat'):
            return
        try:
            self.bot.call('editMessageText', chat_id=message['chat']['id'],
                          message_id=message['message_id'], text=text)
        except APIError as exc:
            LOG.info('Could not edit completed challenge code=%s', exc.code)
