"""Persistent points ledger, daily limits in Asia/Shanghai, and gacha check-in."""
from datetime import datetime, timedelta, timezone
import json
import secrets
import sqlite3
import time

CST = timezone(timedelta(hours=8))
INVITE_DAILY_LIMIT = 10
CHECKIN_PULLS = 10
CHECKIN_POINTS = {3: 0, 4: 1, 5: 5, 6: 10}
FOUR_STAR_RATE = 0.051
FIVE_STAR_RATE = 0.006
FOUR_STAR_PITY = 10
FIVE_STAR_PITY = 90


def format_checkin(amount, results, pity4=None, pity5=None, guaranteed=None):
    """Render one ten-pull; 5 is standard five-star, 6 is featured UP."""
    if not results:
        return f'✅ 签到成功，获得 {amount} 积分！'
    icons = {3: '🔵', 4: '🟣', 5: '🟡', 6: '🟡'}
    counts = {rarity: results.count(rarity) for rarity in (3, 4, 5, 6)}
    sequence = ' '.join(icons.get(rarity, '⚪') for rarity in results)
    parts = []
    if counts[3]:
        parts.append(f'🔵 三星 ×{counts[3]}')
    if counts[4]:
        parts.append(f'🟣 四星 ×{counts[4]}')
    if counts[5]:
        parts.append(f'🟡 五星常驻 ×{counts[5]}')
    if counts[6]:
        parts.append(f'🟡 五星 UP ×{counts[6]}')
    lines = ['✅ 十连签到成功！', f'结果：{sequence}', ' · '.join(parts),
             f'本次获得 {amount} 积分。']
    if pity4 is not None and pity5 is not None:
        progress = f'四星保底进度：{pity4}/{FOUR_STAR_PITY} · 五星保底进度：{pity5}/{FIVE_STAR_PITY}'
        if guaranteed is not None:
            progress += f' · 下次五星大保底：{"是" if guaranteed else "否"}'
        lines.append(progress)
    return '\n'.join(lines)

class Points:
    def __init__(self, db, message_points=1, message_daily_limit=5):
        self.db = db
        self.message_points = int(message_points)
        self.message_daily_limit = int(message_daily_limit)
        db.executescript('''
        CREATE TABLE IF NOT EXISTS point_users(uid INTEGER PRIMARY KEY,name TEXT,total INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS point_events(event TEXT PRIMARY KEY,uid INTEGER,day TEXT,kind TEXT,amount INTEGER);
        CREATE UNIQUE INDEX IF NOT EXISTS point_daily_checkin ON point_events(uid,day) WHERE kind='checkin';
        CREATE INDEX IF NOT EXISTS point_daily_messages ON point_events(uid,day,kind);
        CREATE TABLE IF NOT EXISTS invite_arrivals(uid INTEGER PRIMARY KEY,inviter INTEGER,day TEXT,amount INTEGER NOT NULL);
        CREATE INDEX IF NOT EXISTS invite_daily_totals ON invite_arrivals(inviter,day);
        CREATE TABLE IF NOT EXISTS checkin_pity(
          uid INTEGER PRIMARY KEY,pity4 INTEGER NOT NULL DEFAULT 0,
          pity5 INTEGER NOT NULL DEFAULT 0,guaranteed INTEGER NOT NULL DEFAULT 0,
          updated INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS checkin_draws(
          event TEXT PRIMARY KEY,uid INTEGER,day TEXT,results TEXT NOT NULL,
          total INTEGER NOT NULL,at INTEGER NOT NULL,
          result_notified INTEGER NOT NULL DEFAULT 0,
          broadcast_notified INTEGER NOT NULL DEFAULT 0);
        ''')

    def day(self, timestamp=None):
        return datetime.fromtimestamp(time.time() if timestamp is None else timestamp, CST).date().isoformat()

    @staticmethod
    def _five_star_chance(pity5):
        if pity5 >= FIVE_STAR_PITY:
            return 1.0
        if pity5 >= 74:
            return min(1.0, FIVE_STAR_RATE + 0.06 * (pity5 - 73))
        return FIVE_STAR_RATE

    def _draw_ten(self, pity4, pity5, guaranteed):
        results = []
        high = 0
        for _ in range(CHECKIN_PULLS):
            pity4 += 1
            pity5 += 1
            if high >= 3:
                # A single ten-pull shows at most three four/five-star results.
                results.append(3)
                continue
            roll5 = secrets.randbelow(1000000) / 1000000
            roll4 = secrets.randbelow(1000000) / 1000000
            if pity5 >= FIVE_STAR_PITY or roll5 < self._five_star_chance(pity5):
                pity4 = 0
                pity5 = 0
                high += 1
                if guaranteed:
                    featured = True
                    guaranteed = False
                else:
                    featured = roll4 < 0.5
                    guaranteed = not featured
                results.append(6 if featured else 5)
            elif pity4 >= FOUR_STAR_PITY or roll4 < FOUR_STAR_RATE:
                pity4 = 0
                high += 1
                results.append(4)
            else:
                results.append(3)
        return results, sum(CHECKIN_POINTS[rarity] for rarity in results), pity4, pity5, guaranteed

    def _pity(self, uid):
        row = self.db.execute('SELECT pity4,pity5,guaranteed FROM checkin_pity WHERE uid=?',
                              (uid,)).fetchone()
        return (row[0], row[1], row[2]) if row else (0, 0, 0)

    def checkin_summary(self, uid):
        """Read only persisted draws for this member; never advance pity."""
        counts = {5: 0, 6: 0}
        pulls = 0
        for row in self.db.execute('SELECT results FROM checkin_draws WHERE uid=?', (uid,)):
            results = json.loads(row[0])
            pulls += len(results)
            for rarity in counts:
                counts[rarity] += results.count(rarity)
        pity4, pity5, guaranteed = self._pity(uid)
        return ('🎲 我的签到抽卡记录\n'
                f'累计抽数：{pulls}\n'
                f'累计出金：{counts[5] + counts[6]} 次（五星 UP {counts[6]} 次 · 常驻 {counts[5]} 次）\n'
                f'四星保底进度：{pity4}/{FOUR_STAR_PITY}，距保底最多 {max(0, FOUR_STAR_PITY-pity4)} 抽\n'
                f'五星保底进度：{pity5}/{FIVE_STAR_PITY}，距保底最多 {max(0, FIVE_STAR_PITY-pity5)} 抽\n'
                f'下次五星必为 UP：{"是" if guaranteed else "否（50/50）"}\n'
                '统计仅包含已保存的十连记录。私聊仅查询；每日签到请在群内发送 /checkin。')

    def _stored_draw(self, event):
        row = self.db.execute('SELECT results FROM checkin_draws WHERE event=?', (event,)).fetchone()
        return json.loads(row[0]) if row else None

    def checkin_notified(self, event, field):
        column = {'result': 'result_notified', 'broadcast': 'broadcast_notified'}.get(field)
        if not column:
            return False
        row = self.db.execute(f'SELECT {column} FROM checkin_draws WHERE event=?', (event,)).fetchone()
        return bool(row[0]) if row else False

    def mark_checkin_notified(self, event, field):
        column = {'result': 'result_notified', 'broadcast': 'broadcast_notified'}.get(field)
        if not column:
            return
        with self.db:
            self.db.execute(f'UPDATE checkin_draws SET {column}=1 WHERE event=?', (event,))

    def checkin(self, user, event, timestamp=None):
        """Draw one ten-pull, persist pity and results, and credit points atomically."""
        now = int(time.time() if timestamp is None else timestamp)
        uid = user['id']
        day = self.day(now)
        try:
            with self.db:
                self.db.execute('BEGIN IMMEDIATE')
                previous = self.db.execute('SELECT amount FROM point_events WHERE event=?',
                                           (event,)).fetchone()
                if previous:
                    pity4, pity5, guaranteed = self._pity(uid)
                    return previous[0], 'replay', self._stored_draw(event), pity4, pity5, guaranteed
                count = self.db.execute('SELECT count(*) FROM point_events WHERE uid=? AND day=? AND kind=?',
                                        (uid, day, 'checkin')).fetchone()[0]
                if count >= 1:
                    pity4, pity5, guaranteed = self._pity(uid)
                    return 0, 'limit', None, pity4, pity5, guaranteed
                pity4, pity5, guaranteed = self._pity(uid)
                results, amount, pity4, pity5, guaranteed = self._draw_ten(
                    pity4, pity5, guaranteed)
                name = ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip()
                name = (name or user.get('username') or str(uid))[:128]
                self.db.execute('INSERT INTO point_users(uid,name,total) VALUES(?,?,?) '
                                'ON CONFLICT(uid) DO UPDATE SET name=excluded.name,'
                                'total=point_users.total+excluded.total', (uid, name, amount))
                self.db.execute('INSERT INTO point_events VALUES(?,?,?,?,?)',
                                (event, uid, day, 'checkin', amount))
                self.db.execute('INSERT INTO checkin_draws VALUES(?,?,?,?,?,?,?,?)',
                                (event, uid, day, json.dumps(results), amount, now, 0, 0))
                self.db.execute('INSERT INTO checkin_pity(uid,pity4,pity5,guaranteed,updated) '
                                'VALUES(?,?,?,?,?) ON CONFLICT(uid) DO UPDATE SET '
                                'pity4=excluded.pity4,pity5=excluded.pity5,'
                                'guaranteed=excluded.guaranteed,updated=excluded.updated',
                                (uid, pity4, pity5, guaranteed, now))
                return amount, 'added', results, pity4, pity5, guaranteed
        except sqlite3.IntegrityError:
            # The unique daily index can win a race with a duplicate update.
            self.db.rollback()
            pity4, pity5, guaranteed = self._pity(uid)
            return 0, 'limit', None, pity4, pity5, guaranteed

    def award(self, user, kind, event, timestamp=None):
        if kind == 'checkin':
            amount, status, _, _, _, _ = self.checkin(user, event, timestamp)
            return amount, status
        day = self.day(timestamp)
        name = ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip() or user.get('username') or str(user['id'])
        with self.db:
            self.db.execute('INSERT INTO point_users(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name', (user['id'],name[:128]))
            previous = self.db.execute('SELECT amount FROM point_events WHERE event=?', (event,)).fetchone()
            if previous:
                return previous[0], 'replay'
            if kind == 'message':
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

    def invite(self, user, invited_uid, timestamp=None):
        """Consume the first observed arrival, including ineligible/capped arrivals.

        Keeping claims separate from the ledger
        prevents a points reset or a later rejoin from rewarding the same member.
        """
        day = self.day(timestamp)
        with self.db:
            if self.db.execute('SELECT 1 FROM invite_arrivals WHERE uid=?', (invited_uid,)).fetchone():
                return 0, 'replay'
            eligible = user and not user.get('is_bot') and user['id'] != invited_uid
            uid = user['id'] if eligible else None
            earned = self.invite_daily(uid, timestamp) if eligible else 0
            amount = 1 if eligible and earned < INVITE_DAILY_LIMIT else 0
            self.db.execute('INSERT INTO invite_arrivals VALUES(?,?,?,?)', (invited_uid, uid, day, amount))
            if amount:
                name = ' '.join(str(user.get(k) or '') for k in ('first_name', 'last_name')).strip() or user.get('username') or str(uid)
                self.db.execute('INSERT INTO point_users(uid,name) VALUES(?,?) ON CONFLICT(uid) DO UPDATE SET name=excluded.name', (uid, name[:128]))
                self.db.execute('INSERT INTO point_events VALUES(?,?,?,?,?)',
                                (f'invite:{invited_uid}', uid, day, 'invite', amount))
                self.db.execute('UPDATE point_users SET total=total+? WHERE uid=?', (amount, uid))
            return amount, 'added' if amount else 'ineligible' if not eligible else 'limit'

    def invite_daily(self, uid, timestamp=None):
        return self.db.execute('SELECT COALESCE(sum(amount),0) FROM invite_arrivals WHERE inviter=? AND day=?',
                               (uid, self.day(timestamp))).fetchone()[0]

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
