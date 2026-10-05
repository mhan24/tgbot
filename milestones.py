"""Persistent point-threshold achievements independent of member tags."""
import html
import logging
import time

LOG=logging.getLogger(__name__)

class Milestones:
    def __init__(self,bot):
        self.bot=bot;self.db=bot.db;self.last_scan=0
        self.db.executescript('''CREATE TABLE IF NOT EXISTS point_milestones(
            uid INTEGER,threshold INTEGER,ordinal INTEGER,at INTEGER,total INTEGER,
            sent INTEGER DEFAULT 0,retry_at INTEGER DEFAULT 0,
            PRIMARY KEY(uid,threshold),UNIQUE(threshold,ordinal));''')

    def eligible(self,uid):
        return uid>0 and uid!=self.bot.bot_id and not self.bot.blacklisted(uid)

    def achieve(self,uid):
        if not self.eligible(uid):return
        points=self.db.execute('SELECT total,name FROM point_users WHERE uid=?',(uid,)).fetchone()
        threshold=self.bot.upgrade_points
        if not points or points['total']<threshold:return
        row=self.db.execute('SELECT * FROM point_milestones WHERE uid=? AND threshold=?',(uid,threshold)).fetchone()
        if row and (row['sent'] or row['retry_at']>time.time()):return
        try:
            member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=uid)
            if member.get('status') not in ('member','administrator','creator') and not (member.get('status')=='restricted' and member.get('is_member')):return
            if member.get('user',{}).get('is_bot'):return
            if not row:
                with self.db:
                    self.db.execute('INSERT OR IGNORE INTO point_milestones(uid,threshold,ordinal,at,total) SELECT ?,?,COALESCE(MAX(ordinal),0)+1,?,? FROM point_milestones WHERE threshold=?',
                                    (uid,threshold,int(time.time()),points['total'],threshold))
                row=self.db.execute('SELECT * FROM point_milestones WHERE uid=? AND threshold=?',(uid,threshold)).fetchone()
            with self.db:self.db.execute('UPDATE point_milestones SET retry_at=? WHERE uid=? AND threshold=?',(int(time.time())+60,uid,threshold))
            name=html.escape(points['name'] or str(uid)).replace('@','＠')
            self.bot.send(self.bot.group,f'🎉 恭喜 <a href="tg://user?id={uid}">{name}</a>！\n成为本群第 {row["ordinal"]} 位累计积分达到 {threshold} 分的成员！\n当前积分：{points["total"]} 分。')
            with self.db:self.db.execute('UPDATE point_milestones SET sent=1 WHERE uid=? AND threshold=?',(uid,threshold))
        except Exception as exc:
            LOG.warning('Milestone notification failed uid=%s type=%s',uid,type(exc).__name__)

    def tick(self):
        now=time.time()
        if now-self.last_scan<60:return
        self.last_scan=now
        rows=self.db.execute('''SELECT p.uid FROM point_users p
            LEFT JOIN point_milestones m ON m.uid=p.uid AND m.threshold=?
            WHERE p.total>=? AND (m.uid IS NULL OR (m.sent=0 AND m.retry_at<=?))
            ORDER BY p.uid''',(self.bot.upgrade_points,self.bot.upgrade_points,now)).fetchall()
        for row in rows:self.achieve(row['uid'])
