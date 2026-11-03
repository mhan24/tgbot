"""Mute members after three completed check-in-only days."""
from datetime import datetime,timedelta
import logging
import time
from points import CST

LOG=logging.getLogger('attention')

class Attention:
    def __init__(self,bot):
        self.bot=bot;self.db=bot.db;self.last_check=0
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS activity_days(uid INTEGER,day TEXT,PRIMARY KEY(uid,day));
        CREATE TABLE IF NOT EXISTS attention_bans(uid INTEGER PRIMARY KEY,window_end TEXT,at INTEGER);
        CREATE TABLE IF NOT EXISTS activity_gaps(day TEXT PRIMARY KEY);
        ''')
        now=time.time()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO meta VALUES('activity_started',?)",(str(now),))

    def observed(self,msg):
        """Record a normal group message (not a command) so the day is not counted as silent."""
        user=msg.get('from',{})
        if not user.get('id') or user.get('is_bot') or msg.get('sender_chat'):return
        if self.bot.is_bot_operation(msg) or msg.get('text','').strip().startswith('/'):return
        keys=('text','photo','video','animation','audio','voice','video_note','document','sticker','poll','contact','location','venue','dice')
        if not any(msg.get(k) for k in keys):return
        day=self.bot.points.day(msg.get('date'))
        with self.db:self.db.execute('INSERT OR IGNORE INTO activity_days VALUES(?,?)',(user['id'],day))

    def tick(self,now=None,force=False):
        now=time.time() if now is None else now
        previous=self.db.execute("SELECT value FROM meta WHERE key='activity_heartbeat'").fetchone()
        started=float(self.db.execute("SELECT value FROM meta WHERE key='activity_started'").fetchone()[0])
        last=float(previous[0]) if previous else started
        # A long outage means we cannot prove the absence of speech on those dates.
        with self.db:
            if now-last>300:
                day=datetime.fromtimestamp(last,CST).date();end=datetime.fromtimestamp(now,CST).date()
                while day<=end:
                    self.db.execute('INSERT OR IGNORE INTO activity_gaps VALUES(?)',(day.isoformat(),));day+=timedelta(days=1)
            self.db.execute("INSERT OR REPLACE INTO meta VALUES('activity_heartbeat',?)",(str(now),))
        if not force and now-self.last_check<60:return
        self.last_check=now
        today=datetime.fromtimestamp(now,CST).date()
        end=today-timedelta(days=1);start=today-timedelta(days=3)
        # Deployment day is partial: do not infer past inactivity from the points ledger.
        if start<=datetime.fromtimestamp(started,CST).date():return
        lo,hi=start.isoformat(),end.isoformat()
        if self.db.execute('SELECT 1 FROM activity_gaps WHERE day BETWEEN ? AND ?',(lo,hi)).fetchone():return
        rows=self.db.execute('''SELECT uid FROM point_events
            WHERE kind='checkin' AND day BETWEEN ? AND ?
            AND uid NOT IN (SELECT uid FROM attention_bans WHERE window_end>=?)
            AND uid NOT IN (SELECT uid FROM whitelist)
            AND uid NOT IN (SELECT uid FROM activity_days WHERE day BETWEEN ? AND ?)
            GROUP BY uid HAVING count(DISTINCT day)=3 LIMIT 50''',(lo,hi,lo,lo,hi)).fetchall()
        for row in rows:
            uid=row['uid']
            if uid==self.bot.bot_id or self.bot.whitelisted(uid):continue
            try:
                member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=uid)
                if member['status'] not in ('member','restricted') or (member['status']=='restricted' and not member.get('is_member')):continue
                if member.get('user',{}).get('is_bot'):continue
                # Group default mute length (MUTE_SECONDS); admins can unmute from /manage.
                seconds=self.bot.mute_seconds
                until=int(now)+seconds
                self.bot.restrict(uid, until)
                with self.db:
                    self.db.execute('INSERT INTO warnings(uid,mute_until) VALUES(?,?) '
                                    'ON CONFLICT(uid) DO UPDATE SET mute_until=excluded.mute_until',(uid, until))
                    # Enforcement record: the same window never mutes the same member twice.
                    self.db.execute('INSERT OR REPLACE INTO attention_bans VALUES(?,?,?)',(uid,hi,int(now)))
                self.bot.audit(0,uid,'attention_mute',f'{lo}..{hi}')
                minutes=max(1,seconds//60)
                if minutes>=1440 and minutes%1440==0: span=f'{minutes//1440} 天'
                elif minutes>=60 and minutes%60==0: span=f'{minutes//60} 小时'
                else: span=f'{minutes} 分钟'
                self.bot.send(self.bot.group,f'⚠️ <a href="tg://user?id={uid}">该成员</a> 连续 3 天仅签到未发言，已禁言 {span}。',
                              _delete_after=120 if self.bot.delete_after > 0 else 0)
            except Exception as exc:
                LOG.warning('Attention mute failed uid=%s type=%s',uid,type(exc).__name__)
