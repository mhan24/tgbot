"""管理员按钮面板：每个面板绑定群、操作者和消息，操作后旧按钮失效。"""
import html
import secrets
import time


class Moderation:
    ACTIONS = {'warn', 'mute', 'hour', 'unmute', 'resetwarn', 'ban', 'unban', 'white', 'unwhite'}

    def __init__(self, bot):
        self.bot, self.db = bot, bot.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS moderation_panels(
            token TEXT PRIMARY KEY,actor INTEGER,target INTEGER,chat INTEGER,message INTEGER,
            expires INTEGER,used INTEGER DEFAULT 0,confirm INTEGER DEFAULT 0)''')
        self.db.commit()

    def command(self, msg):
        user = msg.get('from', {})
        if msg.get('sender_chat') or user.get('is_bot') or not user.get('id') or not self.bot.admin(user['id']):
            return True
        chat = msg['chat']['id']
        words = msg.get('text', '').split()[1:]
        reply = msg.get('reply_to_message', {})
        try:
            if words:
                if len(words) != 1:
                    raise ValueError('用法：/manage 用户ID或@用户名，或回复成员消息发送 /manage。')
                target = self.bot.members.resolve(words[0])
            elif reply:
                if reply.get('sender_chat') or not reply.get('from'):
                    raise ValueError('请回复以个人身份发送的成员消息。')
                target = reply['from']['id']
            else:
                target = 0
            self.open(chat, user['id'], target)
        except ValueError as exc:
            self.bot.send(chat, html.escape(str(exc)))
        return True

    def open(self, chat, actor, target=0):
        if target and (target <= 0 or target == self.bot.bot_id or self.bot.admin(target)):
            self.bot.send(chat, '不能对管理员或本机器人执行管理操作。')
            return
        token = secrets.token_hex(6)
        now = int(time.time())
        with self.db:
            self.db.execute('DELETE FROM moderation_panels WHERE expires<?', (now,))
            self.db.execute('INSERT INTO moderation_panels(token,actor,target,chat,expires) VALUES(?,?,?,?,?)',
                            (token, actor, target, chat, now + 300))
        row = self.db.execute('SELECT * FROM moderation_panels WHERE token=?', (token,)).fetchone()
        text, keyboard = self.render(row)
        sent = self.bot.send(chat, text, keep=True, reply_markup={'inline_keyboard': keyboard})
        with self.db:
            self.db.execute('UPDATE moderation_panels SET message=? WHERE token=?', (sent['message_id'], token))
        # Interactive panels stay usable for five minutes, then use the normal deletion queue.
        if chat == self.bot.group:
            self.bot.schedule_delete(chat, sent['message_id'])
            with self.db:
                self.db.execute('UPDATE deletions SET due=? WHERE chat=? AND message_id=?',
                                (now + 300 + self.bot.delete_after, chat, sent['message_id']))

    def render(self, row):
        def button(label, action):
            return {'text': label, 'callback_data': f'mod:{self.bot.group}:{row["token"]}:{action}'}
        close = [button('关闭面板', 'close')]
        target = row['target']
        if not target:
            lines = ['<b>成员管理</b>', '回复成员消息发送 /manage，或填写 ID/@用户名。', '本群黑白名单（各显示最近 20 人）：']
            buttons = [[button('群内机器人', 'bots0')]]
            for table, label in (('blacklist', '黑名单'), ('whitelist', '白名单')):
                entries = self.db.execute(f'SELECT uid FROM {table} ORDER BY at DESC LIMIT 20').fetchall()
                lines.append(f'{label}：' + ('、'.join(str(x['uid']) for x in entries) or '空'))
                for entry in entries:
                    buttons.append([button(f'{label} · {entry["uid"]}', f'u{entry["uid"]}')])
            return '\n'.join(lines), buttons + [close]
        warning = self.db.execute('SELECT * FROM warnings WHERE uid=?', (target,)).fetchone()
        name = self.db.execute('SELECT name FROM point_users WHERE uid=?', (target,)).fetchone()
        label = html.escape(name['name'] if name else str(target))
        muted = bool(warning and warning['mute_until'] > time.time())
        banned, white = self.bot.blacklisted(target), self.bot.whitelisted(target)
        text = (f'<b>成员管理</b> · {label}\nID：{target}\n警告：{warning["count"] if warning else 0}/3\n'
                f'状态：{"黑名单" if banned else "禁言中" if muted else "正常"} · 白名单：{"是" if white else "否"}\n'
                '3 次警告拉黑；解封清零警告。')
        if row['confirm'] == 1:
            return text + '\n确认拉黑？将请求删除该成员全部群消息。', [[button('确认拉黑并删消息', 'ban'), button('取消', 'back')], close]
        if banned:
            return text, [[button('解封并清零警告', 'unban')], close]
        if row['confirm'] == 2:
            return text, [[button('清零警告', 'resetwarn'), button('禁言 1 小时', 'hour')],
                          [button('移出白名单' if white else '加入白名单', 'unwhite' if white else 'white')],
                          [button('返回', 'back'), button('关闭', 'close')]]
        return text, [[button('警告 +1', 'warn'), button('解除禁言' if muted else '禁言', 'unmute' if muted else 'mute')],
                      [button('拉黑并删消息', 'confirm'), button('更多', 'more')], close]

    def bot_list(self,row,page):
        self.bot.members.remember({'id':self.bot.bot_id,'is_bot':True,'first_name':self.bot.username or '本机器人','username':self.bot.username})
        # Bot API cannot enumerate every member. Include bot administrators and observations.
        try:
            for member in self.bot.call('getChatAdministrators',chat_id=self.bot.group,return_bots=True):
                self.bot.members.remember(member.get('user'))
        except Exception:
            pass
        def button(label,action):return {'text':label,'callback_data':f"mod:{self.bot.group}:{row['token']}:{action}"}
        rows=self.db.execute('SELECT * FROM known_bots ORDER BY uid LIMIT 11 OFFSET ?',(page*10,)).fetchall()
        lines=['<b>群内机器人</b>','仅展示已记录及可读取的管理员 Bot，无法枚举完整成员。']
        found=0
        for bot in rows[:10]:
            try:
                member=self.bot.call('getChatMember',chat_id=self.bot.group,user_id=bot['uid'])
                if member.get('status') not in ('member','administrator','creator') and not (member.get('status')=='restricted' and member.get('is_member')):continue
                self.bot.members.remember(member.get('user'))
                state='管理员邀请放行' if bot['approved'] else '已在群内'
            except Exception:
                state='暂无法核实是否在群'
            name=html.escape(bot['name'] or str(bot['uid']))
            username=' @'+html.escape(bot['username']) if bot['username'] else ''
            lines.append(f"{name}{username} · <code>{bot['uid']}</code> · {state}");found+=1
        if not found:lines.append('本页没有查到仍在群内的已记录 Bot。')
        navigation=[]
        if page:navigation.append(button('上一页',f'bots{page-1}'))
        if len(rows)>10:navigation.append(button('下一页',f'bots{page+1}'))
        keyboard=([navigation] if navigation else [])+[[button('刷新',f'bots{page}'),button('关闭','close')]]
        return '\n'.join(lines),keyboard

    def callback(self, query):
        data = query.get('data', '')
        if not data.startswith('mod:'):
            return False
        def answer(text):
            self.bot.call('answerCallbackQuery', callback_query_id=query['id'], text=text)
        fields = data.split(':')
        if len(fields) != 4 or fields[1] != str(self.bot.group):
            answer('此面板不属于当前群。'); return True
        _, _, token, action = fields
        row = self.db.execute('SELECT * FROM moderation_panels WHERE token=?', (token,)).fetchone()
        user, msg = query.get('from', {}), query.get('message', {})
        if (not row or row['used'] or row['expires'] <= time.time() or
                row['chat'] != msg.get('chat', {}).get('id') or row['message'] != msg.get('message_id')):
            answer('面板已失效，请重新发送 /manage。'); return True
        if user.get('is_bot') or user.get('id') != row['actor'] or not self.bot.admin(user['id']):
            answer('仅打开面板的当前管理员可操作。'); return True
        if action == 'close':
            # Invalidate first so stale buttons cannot act even if Telegram deletion fails.
            with self.db:
                self.db.execute('UPDATE moderation_panels SET used=1 WHERE token=?', (token,))
            answer('面板已关闭。')
            try:
                self.bot.call('deleteMessage', chat_id=row['chat'], message_id=row['message'])
            except Exception:
                self.bot.call('editMessageText', chat_id=row['chat'], message_id=row['message'],
                              text='管理面板已关闭。', reply_markup={'inline_keyboard': []})
            else:
                with self.db:
                    self.db.execute('DELETE FROM deletions WHERE chat=? AND message_id=?',
                                    (row['chat'], row['message']))
            return True
        if action.startswith('bots') and action[4:].isdigit() and not row['target']:
            page=int(action[4:])
            if page>10000:answer('页码无效。');return True
            text,keyboard=self.bot_list(row,page)
            try:
                self.bot.call('editMessageText',chat_id=row['chat'],message_id=row['message'],text=text,parse_mode='HTML',reply_markup={'inline_keyboard':keyboard})
            except Exception as exc:
                if 'message is not modified' not in str(exc).lower():raise
            answer('机器人列表已刷新。');return True
        if action.startswith('u') and action[1:].isdigit() and not row['target']:
            target = int(action[1:])
        else:
            target = row['target']
        if target <= 0 or target == self.bot.bot_id or self.bot.admin(target):
            answer('不能管理管理员或本机器人。'); return True
        if action in ('confirm', 'back', 'more'):
            with self.db:
                self.db.execute('UPDATE moderation_panels SET confirm=? WHERE token=?', (1 if action == 'confirm' else 2 if action == 'more' else 0, token))
            row = self.db.execute('SELECT * FROM moderation_panels WHERE token=?', (token,)).fetchone()
            text, keyboard = self.render(row)
            self.bot.call('editMessageText', chat_id=row['chat'], message_id=row['message'], text=text,
                          parse_mode='HTML', reply_markup={'inline_keyboard': keyboard})
            answer('请确认操作。' if action == 'confirm' else '已取消。'); return True
        if action in self.ACTIONS:
            if action == 'ban' and row['confirm'] != 1:
                answer('请先点击拉黑按钮确认。'); return True
            if self.bot.blacklisted(target) and action != 'unban':
                answer('该成员已拉黑，请刷新面板。'); return True
            # Stable token makes warning retries idempotent even after API failure or restart.
            command = 'mute' if action == 'hour' else action
            self.bot._moderation_action({'text': f'/{command} {target}' + (' 60' if action == 'hour' else ''),
                                         'from': {'id': user['id']}, 'chat': {'id': row['chat']}}, f'panel:{token}')
        elif not (action.startswith('u') and action[1:].isdigit() and not row['target']):
            answer('无效操作。'); return True
        with self.db:
            self.db.execute('UPDATE moderation_panels SET used=1 WHERE token=?', (token,))
        # Rotate the token on the same message: repeated old callbacks cannot act twice.
        fresh = secrets.token_hex(6)
        now = int(time.time())
        with self.db:
            self.db.execute('INSERT INTO moderation_panels(token,actor,target,chat,message,expires) VALUES(?,?,?,?,?,?)',
                            (fresh,row['actor'],target,row['chat'],row['message'],now+300))
        current = self.db.execute('SELECT * FROM moderation_panels WHERE token=?',(fresh,)).fetchone()
        text, keyboard = self.render(current)
        try:
            self.bot.call('editMessageText', chat_id=row['chat'], message_id=row['message'],
                          text=text, parse_mode='HTML', reply_markup={'inline_keyboard':keyboard})
        except Exception:
            with self.db:
                self.db.execute('UPDATE moderation_panels SET used=1 WHERE token=?',(fresh,))
            answer('操作已处理，面板更新失败，请重新 /manage 查看。')
            raise
        if row['chat'] == self.bot.group:
            with self.db:
                self.db.execute('UPDATE deletions SET due=? WHERE chat=? AND message_id=?',
                                (now+300+self.bot.delete_after,row['chat'],row['message']))
        answer('面板已更新。')
        return True
