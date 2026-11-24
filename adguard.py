# coding: utf-8
"""广告检查：每条群消息交给 Jev 判定，判为广告就删除并警告，累计 3 次移出本群。

Jev 调用在后台线程里做，不阻塞长轮询；判定结果放进队列，由主循环的维护任务
``drain`` 取出后再查管理员、删消息、记警告、移出成员——SQLite 连接只在主线程使用。
未配置 TYPESAFE_API_KEY 时整个检查关闭；判定失败按“不是广告”处理，不误伤。
"""
import html
import logging
import queue
import threading
import time

from botapi import APIError

LOG = logging.getLogger('groupbot.adguard')

MAX_WARNINGS = 3   # 第 3 次被判为广告即移出本群
WORKERS = 2        # 同时进行的 Jev 判定
BACKLOG = 50       # 排队上限：刷屏时宁可漏判几条，也不无限堆积
SEEN_LIMIT = 500   # 记住最近处理过的消息 ID，编辑后的同一条消息不重复警告


class AdGuard:
    def __init__(self, bot, threshold):
        self.bot = bot
        self.db = bot.db
        self.threshold = threshold
        self.db.execute('CREATE TABLE IF NOT EXISTS ad_warnings('
                        'uid INTEGER PRIMARY KEY, count INTEGER NOT NULL, at INTEGER NOT NULL)')
        self.jobs = queue.Queue(BACKLOG)
        self.results = queue.Queue()
        self._lock = threading.Lock()
        self._inflight = 0
        self._workers = []
        self._seen = {}

    @property
    def enabled(self):
        return self.bot.ai.ad_enabled

    # --- 主线程：收集待判定的消息 ----------------------------------------

    def submit(self, message):
        """把一条群消息（含编辑）交给后台判定；不需要检查的直接忽略。"""
        if not self.enabled or (message.get('chat') or {}).get('id') != self.bot.group:
            return
        text = (message.get('text') or message.get('caption') or '').strip()
        sender = message.get('from') or {}
        uid = int(sender.get('id') or 0)
        if (not text or not uid or message.get('sender_chat') or sender.get('is_bot')
                or uid == self.bot.bot_id):
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
        who = self._mention(job)
        if count < MAX_WARNINGS:
            self.bot.send(job['chat'], f'⚠️ {who} 的消息被判定为广告，已删除。'
                                       f'警告 {count}/{MAX_WARNINGS}，'
                                       f'累计 {MAX_WARNINGS} 次将被移出本群。')
            return
        self._kick(uid)
        with self.db:
            self.db.execute('DELETE FROM ad_warnings WHERE uid=?', (uid,))
        self.bot.audit(0, uid, 'ad_kick', str(count))
        self.bot.send(job['chat'], f'🚫 {who} 已累计 {count} 次发布广告，已被移出本群。')

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

    @staticmethod
    def _mention(job):
        user = job['user']
        name = str(user.get('first_name') or '').strip() or '成员'
        return f'<a href="tg://user?id={job["uid"]}">{html.escape(name)}</a>'
