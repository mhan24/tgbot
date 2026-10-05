"""Persistent points ledger, daily limits in Asia/Shanghai."""
from datetime import datetime, timedelta, timezone
import secrets
import time

CST = timezone(timedelta(hours=8))

class Points:
    def __init__(self, db, checkin_min=1, checkin_max=5, message_points=1, message_daily_limit=5):
        self.db = db
        self.checkin_min = int(checkin_min)
        self.checkin_max = int(checkin_max)
        self.message_points = int(message_points)
        self.message_daily_limit = int(message_daily_limit)
        db.executescript('''
        CREATE TABLE IF NOT EXISTS point_users(uid INTEGER PRIMARY KEY,name TEXT,total INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS point_events(event TEXT PRIMARY KEY,uid INTEGER,day TEXT,kind TEXT,amount INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS point_daily_checkin ON point_events(uid,day) WHERE kind='checkin';
        CREATE INDEX IF NOT EXISTS point_daily_messages ON point_events(uid,day,kind);
        ''')

    def day(self, timestamp=None):
        return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, CST).date().isoformat()

    def award(self, user, kind, event, timestamp=None):
        day = self.day(timestamp)
        name = ' '.join(str(user.get(k) or '') for k in ('first_name','last_name')).strip() or user.get('username') or str(user['id'])
        with self.db:
            self.db.execute('INSERT INTO point_users(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name', (user['id'],name[:128]))
            previous = self.db.execute('SELECT amount FROM point_events WHERE event=?', (event,)).fetchone()
            if previous:
                return previous[0], 'replay'
            if kind == 'checkin':
                count = self.db.execute('SELECT count(*) FROM point_events WHERE uid=? AND day=? AND kind=?',
                                        (user['id'], day, kind)).fetchone()[0]
                if count >= 1:
                    return 0, 'limit'
                amount = secrets.randbelow(self.checkin_max - self.checkin_min + 1) + self.checkin_min
            elif kind == 'message':
                earned = self.db.execute('SELECT COALESCE(sum(amount),0) FROM point_events '
                                         'WHERE uid=? AND day=? AND kind=?',
                                         (user['id'], day, kind)).fetchone()[0]
                if earned >= self.message_daily_limit:
                    return 0, 'limit'
                amount = min(self.message_points, self.message_daily_limit - earned)
            else:
                raise ValueError(f'unsupported points event kind: {kind}')
            self.db.execute('INSERT INTO point_events VALUES(?,?,?,?,?)', (event,user['id'],day,kind,amount))
            self.db.execute('UPDATE point_users SET total=total+? WHERE uid=?', (amount,user['id']))
            return amount, 'added'

    def reset(self, uid):
        """Clear a member's entire ledger; used when the member is automatically blacklisted."""
        with self.db:
            self.db.execute('DELETE FROM point_events WHERE uid=?', (uid,))
            self.db.execute('UPDATE point_users SET total=0 WHERE uid=?', (uid,))

    def stats(self, uid, timestamp=None):
        row = self.db.execute('SELECT total FROM point_users WHERE uid=?', (uid,)).fetchone()
        rows = self.db.execute('SELECT kind,sum(amount) FROM point_events WHERE uid=? AND day=? GROUP BY kind', (uid,self.day(timestamp))).fetchall()
        daily = dict(rows)
        total = row[0] if row else 0
        rank = 1+self.db.execute('SELECT count(*) FROM point_users WHERE total>?', (total,)).fetchone()[0] if total else None
        return total, daily.get('checkin',0), daily.get('message',0), rank

    def leaderboard(self, limit=10):
        sql = 'SELECT uid,name,total FROM point_users WHERE total>0 ORDER BY total DESC,uid ASC'
        params = ()
        if limit is not None:
            sql += ' LIMIT ?'
            params = (limit,)
        return self.db.execute(sql, params).fetchall()

    def leaderboard_page(self, page=0, size=50):
        with self.db:
            total = self.db.execute('SELECT count(*) FROM point_users WHERE total>0').fetchone()[0]
            pages = max(1, (total + size - 1) // size)
            page = min(max(0, page), pages - 1)
            rows = self.db.execute('SELECT uid,name,total,RANK() OVER (ORDER BY total DESC) AS rank '
                                   'FROM point_users WHERE total>0 ORDER BY total DESC,uid ASC LIMIT ? OFFSET ?',
                                   (size, page * size)).fetchall()
        return rows, total, page, pages
