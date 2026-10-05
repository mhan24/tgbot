"""Observed Telegram user names, always revalidated before resolving a target."""
import re
import time

class Members:
    def __init__(self, bot):
        self.bot=bot;self.db=bot.db
        self.db.execute('CREATE TABLE IF NOT EXISTS member_names(uid INTEGER PRIMARY KEY,username TEXT,at INTEGER)')
        self.db.execute('CREATE INDEX IF NOT EXISTS member_names_username ON member_names(username)')
        self.db.commit()

    def remember(self,user):
        if not isinstance(user,dict) or not isinstance(user.get('id'),int) or user['id']<=0:return
        name=(user.get('username') or '').lower()
        with self.db:
            if name:self.db.execute('UPDATE member_names SET username=? WHERE username=? AND uid!=?',('',name,user['id']))
            self.db.execute('INSERT OR REPLACE INTO member_names VALUES(?,?,?)',(user['id'],name,int(time.time())))

    def observe(self,update):
        for key in ('message','edited_message'):
            msg=update.get(key,{})
            if msg.get('chat',{}).get('id')!=self.bot.group and msg.get('chat',{}).get('type')!='private':continue
            if not msg.get('sender_chat'):self.remember(msg.get('from'))
            for user in msg.get('new_chat_members',[]):self.remember(user)
            self.remember(msg.get('left_chat_member'))
        for key in ('chat_member','chat_join_request'):
            event=update.get(key,{})
            if event.get('chat',{}).get('id')!=self.bot.group:continue
            self.remember(event.get('new_chat_member',{}).get('user') if key=='chat_member' else event.get('from'))
        self.remember(update.get('callback_query',{}).get('from'))

    def resolve(self,value):
        if value.isascii() and value.isdigit() and 0<int(value)<2**52:return int(value)
        if not re.fullmatch(r'@[A-Za-z0-9_]{1,32}',value):raise ValueError('请提供数字 ID 或 @用户名。')
        name=value[1:].lower()
        rows=self.db.execute('SELECT uid FROM member_names WHERE username=?',(name,)).fetchall()
        if not rows:
            raise ValueError('尚未收录该用户名。请让对方在群里发言，或回复其消息执行。')
        for row in rows:
            member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=row['uid'])
            user=member.get('user',{})
            self.remember(user)
            if user.get('id')==row['uid'] and (user.get('username') or '').lower()==name:return row['uid']
        raise ValueError('该用户名已变更或无法核实，请回复目标成员的消息执行。')

