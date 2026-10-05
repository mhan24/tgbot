"""Private unban requests; only the current group owner can decide."""
import html
import logging
import time

LOG = logging.getLogger(__name__)

class Appeals:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS appeal_drafts(uid INTEGER PRIMARY KEY);
        CREATE TABLE IF NOT EXISTS appeals(
          id INTEGER PRIMARY KEY,uid INTEGER,reason TEXT,ban_at INTEGER,
          created INTEGER,state TEXT DEFAULT 'pending',notified INTEGER DEFAULT 0,
          retry_at INTEGER DEFAULT 0,result_sent INTEGER DEFAULT 0);
        ''')

        columns = {row[1] for row in self.db.execute('PRAGMA table_info(appeals)')}
        for column in ('username', 'display_name'):
            if column not in columns:
                self.db.execute(f"ALTER TABLE appeals ADD COLUMN {column} TEXT NOT NULL DEFAULT ''")
        self.db.commit()

    def applicant(self, row):
        username = row['username']
        if not username:
            known = self.db.execute('SELECT username FROM member_names WHERE uid=?', (row['uid'],)).fetchone()
            username = known['username'] if known else ''
        name = row['display_name'] or username or str(row['uid'])
        mention = f'<a href="tg://user?id={row["uid"]}">{html.escape(name)}</a>'
        return f'申请人：{mention}\n用户名：{html.escape("@" + username) if username else "未设置"}\n用户 ID：<code>{row["uid"]}</code>'

    def owner(self):
        return next(m['user']['id'] for m in self.bot.call('getChatAdministrators',chat_id=self.bot.group)
                    if m['status']=='creator' and not m['user'].get('is_bot'))

    def notify(self,row):
        owner = self.owner()
        self.bot.send(owner, f'🔓 解封申请 #{row["id"]}\n{self.applicant(row)}\n理由：{html.escape(row["reason"])}',
                      reply_markup={'inline_keyboard':[[
                          {'text':'批准解封','callback_data':f'appeal:approve:{self.bot.group}:{row["id"]}'},
                          {'text':'拒绝申请','callback_data':f'appeal:reject:{self.bot.group}:{row["id"]}'}]]})
        with self.db:self.db.execute('UPDATE appeals SET notified=1 WHERE id=?',(row['id'],))

    def command(self,msg):
        if msg.get('chat',{}).get('type')!='private':return False
        uid=msg['from']['id'];text=msg.get('text','').strip()
        cmd=text.split(maxsplit=1)[0].split('@')[0] if text else ''
        if cmd=='/appeals':
            if uid!=self.owner():
                self.bot.send(uid,'仅群主可查看解封申请。');return True
            rows=self.db.execute("SELECT * FROM appeals WHERE state='pending' ORDER BY id LIMIT 20").fetchall()
            if not rows:self.bot.send(uid,'暂无待处理解封申请。')
            for row in rows:self.notify(row)
            return True
        draft=self.db.execute('SELECT 1 FROM appeal_drafts WHERE uid=?',(uid,)).fetchone()
        if cmd=='/cancel' and draft:
            with self.db:self.db.execute('DELETE FROM appeal_drafts WHERE uid=?',(uid,))
            self.bot.send(uid,'已取消填写。');return True
        if cmd not in ('/appeal','/start') and not (draft and not text.startswith('/')):return False
        ban=self.db.execute('SELECT * FROM blacklist WHERE uid=?',(uid,)).fetchone()
        if not ban:
            if cmd=='/start':return False
            self.bot.send(uid,'你当前不在黑名单，无需申请解封。');return True
        if cmd=='/start':
            self.bot.send(uid,'你当前已被拉黑。发送 /appeal 解封理由，或先发送 /appeal 再填写理由，由群主审核。');return True
        pending=self.db.execute("SELECT id FROM appeals WHERE uid=? AND state='pending'",(uid,)).fetchone()
        if pending:
            self.bot.send(uid,f'申请 #{pending[0]} 正在等待群主处理，请勿重复提交。');return True
        last=self.db.execute('SELECT created FROM appeals WHERE uid=? ORDER BY id DESC LIMIT 1',(uid,)).fetchone()
        if last and time.time()-last[0]<86400:
            self.bot.send(uid,'每天最多提交一次解封申请，请在上次提交满 24 小时后重试。');return True
        reason=text.split(maxsplit=1)[1] if cmd=='/appeal' and len(text.split(maxsplit=1))==2 else (text if draft and cmd!='/appeal' else '')
        if not reason:
            with self.db:self.db.execute('INSERT OR IGNORE INTO appeal_drafts VALUES(?)',(uid,))
            self.bot.send(uid,'请发送解封理由（1–1000 字），发送 /cancel 取消。');return True
        if len(reason)>1000:
            self.bot.send(uid,'理由请控制在 1000 字以内。');return True
        with self.db:
            user = msg['from']
            name = ' '.join(filter(None, (user.get('first_name'), user.get('last_name'))))
            cur=self.db.execute('INSERT INTO appeals(uid,reason,ban_at,created,username,display_name) VALUES(?,?,?,?,?,?)',
                                (uid,reason,ban['at'],int(time.time()),user.get('username') or '',name))
            self.db.execute('DELETE FROM appeal_drafts WHERE uid=?',(uid,))
        self.bot.send(uid,f'解封申请 #{cur.lastrowid} 已保存，等待群主审核。批准后需重新申请入群并完成验证。')
        self.tick()
        return True

    def callback(self,q):
        data=q.get('data','').split(':')
        if not data or data[0]!='appeal':return False
        def answer(t):self.bot.call('answerCallbackQuery',callback_query_id=q['id'],text=t,show_alert=True)
        if len(data)==4:
            if not data[2].lstrip('-').isdigit() or int(data[2])!=self.bot.group or not data[3].isdigit():
                answer('该申请属于其他群，请在对应群组的管理上下文中处理。');return True
            aid=int(data[3])
        elif len(data)==3 and data[2].isdigit():
            aid=int(data[2])
        else:
            answer('无效申请。');return True
        if data[1] not in ('approve','reject'):
            answer('无效申请。');return True
        if q['from']['id']!=self.owner():
            answer('仅当前群主可处理。');return True
        row=self.db.execute('SELECT * FROM appeals WHERE id=?',(aid,)).fetchone()
        if not row or row['state']!='pending':
            answer('申请已处理或不存在。');return True
        ban=self.db.execute('SELECT * FROM blacklist WHERE uid=?',(row['uid'],)).fetchone()
        state='approved' if data[1]=='approve' else 'rejected'
        if not ban or ban['at']!=row['ban_at']:state='expired'
        if state=='approved':
            try:self.bot.call('unbanChatMember',chat_id=self.bot.group,user_id=row['uid'],only_if_banned=True)
            except Exception:
                answer('解封失败，申请仍待处理，请稍后重试。');return True
        with self.db:
            if state=='approved':
                self.db.execute('DELETE FROM blacklist WHERE uid=?',(row['uid'],))
                self.db.execute('DELETE FROM verification WHERE uid=?',(row['uid'],))
                self.db.execute('UPDATE warnings SET count=0,mute_until=0 WHERE uid=?',(row['uid'],))
            self.db.execute('UPDATE appeals SET state=?,retry_at=0 WHERE id=?',(state,row['id']))
        self.bot.audit(q['from']['id'],row['uid'],'appeal_'+state,str(row['id']))
        answer({'approved':'已批准解封。','rejected':'已拒绝。','expired':'封禁状态已变化，旧申请已失效。'}[state])
        self.tick()
        return True

    def tick(self):
        now=int(time.time())
        rows=self.db.execute("SELECT * FROM appeals WHERE retry_at<=? AND ((state='pending' AND notified=0) OR (state!='pending' AND result_sent=0)) LIMIT 5",(now,)).fetchall()
        for row in rows:
            with self.db:self.db.execute('UPDATE appeals SET retry_at=? WHERE id=?',(now+300,row['id']))
            try:
                if row['state']=='pending':self.notify(row)
                else:
                    result={'approved':'群主已批准解封。请重新申请入群并完成验证：https://t.me/setupode',
                            'rejected':'群主已拒绝本次解封申请。','expired':'封禁状态已变化，本次申请已失效。'}[row['state']]
                    self.bot.send(row['uid'],f'申请 #{row["id"]}：{result}')
                    with self.db:self.db.execute('UPDATE appeals SET result_sent=1 WHERE id=?',(row['id'],))
            except Exception as exc:LOG.warning('Appeal delivery failed id=%s type=%s',row['id'],type(exc).__name__)
