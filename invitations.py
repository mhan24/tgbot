"""Private personal invitation links; attribution is persisted before approval."""
import html
import json
import time
from points import INVITE_DAILY_LIMIT


class Invitations:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.executescript('''
        CREATE TABLE IF NOT EXISTS invite_links(uid INTEGER PRIMARY KEY,link TEXT UNIQUE NOT NULL,user_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS invite_requests(uid INTEGER PRIMARY KEY,inviter INTEGER NOT NULL,expires INTEGER NOT NULL);
        ''')

    def command(self, msg):
        words = msg.get('text', '').split()
        if not words:
            return False
        command = words[0].lower().split('@', 1)
        if command[0] != '/invite' or (len(command) == 2 and command[1] != self.bot.username.lower()):
            return False
        chat, user = msg['chat'], msg.get('from', {})
        if chat.get('type') != 'private':
            self.bot.send(chat['id'], '请私聊机器人发送 /invite，领取自己的专属邀请链接。',
                          _delete_after=self.bot.notice_seconds())
            return True
        uid = user.get('id')
        if not uid or user.get('is_bot'):
            return True
        try:
            member = self.bot.call('getChatMember', chat_id=self.bot.group, user_id=uid)
            active = member.get('status') in ('creator','administrator','member') or (member.get('status') == 'restricted' and member.get('is_member'))
            pending = self.db.execute("SELECT 1 FROM verification WHERE uid=? AND kind!='approved'", (uid,)).fetchone()
            if not active or self.bot.blacklisted(uid) or pending:
                self.bot.send(uid, '请先加入 @setupode 并完成入群验证，再发送 /invite。本次未创建链接、未加分。')
                return True
            row = self.db.execute('SELECT link FROM invite_links WHERE uid=?', (uid,)).fetchone()
            if row:
                link = row['link']
            else:
                result = self.bot.call('createChatInviteLink', chat_id=self.bot.group,
                                       name=f'invite:{uid}', creates_join_request=True)
                link = result['invite_link']
                if not isinstance(link, str) or not link.startswith('https://t.me/'):
                    raise ValueError('Invalid invitation link')
                snapshot = {k:user[k] for k in ('id','first_name','last_name','username') if k in user}
                with self.db:
                    self.db.execute('INSERT INTO invite_links VALUES(?,?,?)', (uid,link,json.dumps(snapshot,ensure_ascii=False)))
        except Exception:
            self.bot.send(uid, '暂时无法领取邀请链接，请稍后重新发送 /invite；本次未加分。', _delete_after=0)
            return True
        self.bot.send(uid, f'📍 邀请加入 @setupode\n你的专属邀请链接：{html.escape(link)}\n'
                      f'成功邀请一位真人进群得 1 分，每天最多 {INVITE_DAILY_LIMIT} 分，北京时间零点重置。\n'
                      '同一成员只计一次；转发此专属链接仍归你，普通共享链接不计分。\n'
                      f'今日邀请积分：{self.bot.points.invite_daily(uid)}/{INVITE_DAILY_LIMIT}\n发送 /points 查看余额。', _delete_after=0)
        return True

    def request(self, event):
        link = (event.get('invite_link') or {}).get('invite_link')
        row = self.db.execute('SELECT uid FROM invite_links WHERE link=?', (link,)).fetchone()
        with self.db:
            self.db.execute('DELETE FROM invite_requests WHERE expires<=?', (int(time.time()),))
            if row:
                self.db.execute('INSERT INTO invite_requests VALUES(?,?,?) ON CONFLICT(uid) DO UPDATE SET inviter=excluded.inviter,expires=excluded.expires',
                                (event['from']['id'],row['uid'],int(event.get('date') or time.time())+86400))
            else:
                self.db.execute('DELETE FROM invite_requests WHERE uid=?', (event['from']['id'],))

    def inviter(self, event):
        info = event.get('invite_link')
        if info:
            row = self.db.execute('SELECT user_json FROM invite_links WHERE link=?', (info.get('invite_link'),)).fetchone()
            return json.loads(row[0]) if row else None
        if event.get('via_join_request') or event.get('from', {}).get('id') == self.bot.bot_id:
            row = self.db.execute('SELECT l.user_json FROM invite_requests r JOIN invite_links l ON l.uid=r.inviter WHERE r.uid=? AND r.expires>?',
                                  (event['new_chat_member']['user']['id'],int(event.get('date') or time.time()))).fetchone()
            return json.loads(row[0]) if row else None
        if event.get('via_chat_folder_invite_link'):
            return None
        return event.get('from')
