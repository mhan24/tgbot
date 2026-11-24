# coding: utf-8
"""广告检查：每条群消息交给 Jev 判定，判为广告就删除并警告，累计 3 次移出本群。

Jev 调用在后台线程里做，不阻塞长轮询；判定结果放进队列，由主循环的维护任务
``drain`` 取出后再查管理员、删消息、记警告、移出成员——SQLite 连接只在主线程使用。
未配置 TYPESAFE_API_KEY 时整个检查关闭；判定失败按“不是广告”处理，不误伤。

管理员可用 /whitelist 把成员加入白名单（不再检查其消息），用 /clearwarn 清空警告。
"""
import html
import logging
import queue
import re
import threading
import time

from botapi import APIError

LOG = logging.getLogger('groupbot.adguard')

MAX_WARNINGS = 3   # 第 3 次被判为广告即移出本群
WORKERS = 2        # 同时进行的 Jev 判定
BACKLOG = 50       # 排队上限：刷屏时宁可漏判几条，也不无限堆积
SEEN_LIMIT = 500   # 记住最近处理过的消息 ID，编辑后的同一条消息不重复警告
USAGE = ('<b>广告检查管理</b>（群主和管理员可用）\n'
         '/whitelist add — 回复成员的消息，或 /whitelist add 用户ID\n'
         '/whitelist remove — 同上，移出白名单\n'
         '/whitelist list — 查看白名单\n'
         '/clearwarn — 回复成员的消息，或 /clearwarn 用户ID，清空其警告\n'
         '/clearwarn all — 清空所有人的警告')
NO_TARGET = '请回复要操作的成员的消息，或在命令后写用户 ID（私聊里只能用用户 ID）。'


class AdGuard:
    def __init__(self, bot, threshold):
        self.bot = bot
        self.db = bot.db
        self.threshold = threshold
        self.db.execute('CREATE TABLE IF NOT EXISTS ad_warnings('
                        'uid INTEGER PRIMARY KEY, count INTEGER NOT NULL, at INTEGER NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS ad_whitelist('
                        'uid INTEGER PRIMARY KEY, name TEXT NOT NULL, actor INTEGER NOT NULL, '
                        'at INTEGER NOT NULL)')
        self.jobs = queue.Queue(BACKLOG)
        self.results = queue.Queue()
        self._lock = threading.Lock()
        self._inflight = 0
        self._workers = []
        self._seen = {}

    @property
    def enabled(self):
        return self.bot.ai.ad_enabled

    def whitelisted(self, uid):
        return bool(self.db.execute('SELECT 1 FROM ad_whitelist WHERE uid=?', (uid,)).fetchone())

    # --- 主线程：收集待判定的消息 ----------------------------------------

    def submit(self, message):
        """把一条群消息（含编辑）交给后台判定；不需要检查的直接忽略。"""
        if not self.enabled or (message.get('chat') or {}).get('id') != self.bot.group:
            return
        text = (message.get('text') or message.get('caption') or '').strip()
        sender = message.get('from') or {}
        uid = int(sender.get('id') or 0)
        if (not text or not uid or message.get('sender_chat') or sender.get('is_bot')
                or uid == self.bot.bot_id or self.whitelisted(uid)):
            return
        job = {'chat': self.bot.group, 'message_id': int(message.get('message_id') or 0),
               'uid': uid, 'user': sender, 'text': text}
        with self._lock:
            self._inflight += 1
        try:
            self.jobs.put_nowait(job)
        except queue.Full:
            with self._lock:
                self._inflight -= 1
            LOG.warning('Ad check backlog full; skipped message uid=%s', uid)
            return
        self._start_workers()

    def _start_workers(self):
        while len(self._workers) < WORKERS:
            worker = threading.Thread(target=self._work, daemon=True)
            try:
                worker.start()
            except RuntimeError:
                LOG.warning('Cannot start ad check worker')
                return
            self._workers.append(worker)

    def busy(self):
        """还有判定在进行或结果未处理：主循环据此缩短长轮询，及时处置。"""
        with self._lock:
            return self._inflight > 0

    # --- 后台线程：只发 HTTP 请求 ----------------------------------------

    def _work(self):
        while True:
            job = self.jobs.get()
            try:
                verdict = self.bot.ai.ad_verdict(job['text'], self.threshold)
            except Exception:
                LOG.exception('Ad check failed')
                verdict = None
            self.results.put((job, verdict))
            self.jobs.task_done()

    # --- 主线程：处置判定结果 --------------------------------------------

    def drain(self):
        """维护任务：处理所有已返回的判定结果。单条失败只记日志，不影响其余。"""
        while True:
            try:
                job, verdict = self.results.get_nowait()
            except queue.Empty:
                return
            try:
                if verdict and verdict[0]:
                    self._punish(job, verdict[1])
            except APIError as exc:
                LOG.warning('Ad handling failed uid=%s code=%s', job['uid'], exc.code)
            finally:
                with self._lock:
                    self._inflight = max(0, self._inflight - 1)

    def _punish(self, job, probability):
        uid, message_id = job['uid'], job['message_id']
        if self.whitelisted(uid):
            # 判定期间被管理员加入了白名单。
            return
        if message_id in self._seen:
            # 同一条消息编辑后再次被判为广告：只删除，不重复计警告。
            self._delete(job)
            return
        if self.bot.admin(uid):
            return
        self._remember(message_id)
        self._delete(job)
        with self.db:
            row = self.db.execute('SELECT count FROM ad_warnings WHERE uid=?', (uid,)).fetchone()
            count = (row[0] if row else 0) + 1
            self.db.execute('INSERT INTO ad_warnings(uid, count, at) VALUES(?,?,?) '
                            'ON CONFLICT(uid) DO UPDATE SET count=excluded.count, at=excluded.at',
                            (uid, count, int(time.time())))
        self.bot.audit(0, uid, 'ad_warning', f'{count} {probability:.2f} {job["text"][:200]}')
        who = self._who(uid, job['user'])
        score = self._score(probability)
        if count < MAX_WARNINGS:
            self.bot.send(job['chat'], f'⚠️ {who} 的消息被判定为广告，已删除。\n'
                                       f'警告 {count}/{MAX_WARNINGS}，'
                                       f'累计 {MAX_WARNINGS} 次将被移出本群。\n{score}')
            return
        self._kick(uid)
        with self.db:
            self.db.execute('DELETE FROM ad_warnings WHERE uid=?', (uid,))
        self.bot.audit(0, uid, 'ad_kick', str(count))
        self.bot.send(job['chat'], f'🚫 {who} 已累计 {count} 次发布广告，已被移出本群。\n{score}')

    def _delete(self, job):
        try:
            self.bot.call('deleteMessage', chat_id=job['chat'], message_id=job['message_id'])
        except APIError as exc:
            # 消息可能已被管理员或本人删掉：不影响警告。
            LOG.info('Ad message delete failed uid=%s code=%s', job['uid'], exc.code)

    def _kick(self, uid):
        """移出本群：先 ban 再解封，成员被移出，之后可重新申请（需重新验证）。"""
        self.bot.call('banChatMember', chat_id=self.bot.group, user_id=uid)
        # 验证超时会先封禁再解封，移出后取消未完成的验证。
        self.bot.verification.forget(uid)
        try:
            self.bot.call('unbanChatMember', chat_id=self.bot.group, user_id=uid,
                          only_if_banned=True)
        except APIError as exc:
            LOG.warning('Unban after ad kick failed uid=%s code=%s', uid, exc.code)

    def _remember(self, message_id):
        if len(self._seen) >= SEEN_LIMIT:
            for key in list(self._seen)[:SEEN_LIMIT // 2]:
                del self._seen[key]
        self._seen[message_id] = True

    def _score(self, probability):
        """判定依据：Jev 模型与广告概率，以及本群使用的阈值。"""
        return (f'🧠 {html.escape(self.bot.ai.typesafe_model)} 判定值 {probability:.2f}'
                f'（阈值 {self.threshold:.2f}）')

    @staticmethod
    def _who(uid, user):
        name = str((user or {}).get('first_name') or '').strip() or str(uid)
        return f'<a href="tg://user?id={uid}">{html.escape(name)}</a>'

    # --- 管理命令：白名单与清空警告 --------------------------------------

    def command(self, message, name):
        """/whitelist 与 /clearwarn；群聊和私聊都可用，只有群主和管理员能操作。"""
        chat = message['chat']['id']
        actor = int((message.get('from') or {}).get('id') or 0)
        if not actor or not self.bot.admin(actor):
            self.bot.send(chat, '只有群主和群管理员可以管理广告检查。')
            return
        args = (message.get('text') or '').split()[1:]
        if name == '/whitelist':
            self._whitelist(chat, actor, message, args)
        else:
            self._clear(chat, actor, message, args)

    def _target(self, message, args):
        """操作对象：命令里的数字用户 ID 优先，否则取被回复消息的发送者。返回 (uid, user)。"""
        if args:
            if not re.fullmatch(r'\d{1,20}', args[0]):
                return 0, {}
            uid = int(args[0])
            return uid, self._lookup(uid)
        reply = message.get('reply_to_message') or {}
        user = reply.get('from') or {}
        # 话题群里普通消息也挂在话题首帖下，那不是真正的“回复某人”。
        if (reply.get('sender_chat') or user.get('is_bot') or reply.get('forum_topic_created')
                or int(user.get('id') or 0) == self.bot.bot_id):
            return 0, {}
        return int(user.get('id') or 0), user

    def _lookup(self, uid):
        """按用户 ID 查名字（只为显示）；查不到不影响操作。"""
        try:
            return (self.bot.call('getChatMember', chat_id=self.bot.group,
                                  user_id=uid) or {}).get('user') or {}
        except APIError as exc:
            LOG.info('Whitelist lookup failed uid=%s code=%s', uid, exc.code)
            return {}

    def _whitelist(self, chat, actor, message, args):
        action = args[0].lower() if args else ''
        if action == 'list' and len(args) == 1:
            self._list_whitelist(chat)
            return
        if action not in ('add', 'remove'):
            self.bot.send(chat, USAGE)
            return
        uid, user = self._target(message, args[1:])
        if not uid:
            self.bot.send(chat, NO_TARGET)
            return
        who = self._who(uid, user)
        if action == 'remove':
            with self.db:
                removed = self.db.execute('DELETE FROM ad_whitelist WHERE uid=?', (uid,)).rowcount
            if removed:
                self.bot.audit(actor, uid, 'ad_whitelist_remove')
            self.bot.send(chat, f'已将 {who} 移出广告白名单，之后的消息会照常检查。'
                          if removed else f'{who} 不在广告白名单中。')
            return
        if self.whitelisted(uid):
            self.bot.send(chat, f'{who} 已在广告白名单中。')
            return
        name = self.bot.display_name(user) if user else ''
        with self.db:
            self.db.execute('INSERT INTO ad_whitelist(uid, name, actor, at) VALUES(?,?,?,?)',
                            (uid, name, actor, int(time.time())))
            cleared = self.db.execute('DELETE FROM ad_warnings WHERE uid=?', (uid,)).rowcount
        self.bot.audit(actor, uid, 'ad_whitelist_add')
        self.bot.send(chat, f'✅ 已将 {who} 加入广告白名单，不再检查其消息'
                      + ('，原有警告已清空。' if cleared else '。'))

    def _list_whitelist(self, chat):
        rows = self.db.execute('SELECT uid, name FROM ad_whitelist ORDER BY at, uid').fetchall()
        if not rows:
            self.bot.send(chat, '广告白名单是空的。回复成员的消息发送 /whitelist add 添加。')
            return
        page, size = ['<b>广告白名单</b>'], 5
        for uid, name in rows:
            line = f'{html.escape(name or "成员")} <code>{uid}</code>'
            length = len(line.encode('utf-16-le')) // 2
            if size + length + 1 > 4096:
                self.bot.send(chat, '\n'.join(page))
                page, size = [], 0
            page.append(line)
            size += length + 1
        self.bot.send(chat, '\n'.join(page))

    def _clear(self, chat, actor, message, args):
        if args and args[0].lower() == 'all' and len(args) == 1:
            with self.db:
                cleared = self.db.execute('DELETE FROM ad_warnings').rowcount
            self.bot.audit(actor, 0, 'ad_clear', 'all')
            self.bot.send(chat, f'已清空所有人的广告警告（{cleared} 人）。')
            return
        uid, user = self._target(message, args)
        if not uid:
            self.bot.send(chat, USAGE if not args and not message.get('reply_to_message')
                          else NO_TARGET)
            return
        who = self._who(uid, user)
        with self.db:
            row = self.db.execute('SELECT count FROM ad_warnings WHERE uid=?', (uid,)).fetchone()
            self.db.execute('DELETE FROM ad_warnings WHERE uid=?', (uid,))
        if not row:
            self.bot.send(chat, f'{who} 没有广告警告。')
            return
        self.bot.audit(actor, uid, 'ad_clear', str(row[0]))
        self.bot.send(chat, f'已清空 {who} 的 {row[0]} 次广告警告。')
