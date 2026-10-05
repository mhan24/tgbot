"""Bounded recent group-chat history, used to give the AI conversation context."""
import html
import json
import logging
import re
import time

LOG = logging.getLogger('history')
TAGS = re.compile(r'<[^>]+>')

class History:
    def __init__(self, bot, keep_messages=50, keep_seconds=86400, max_chars=1000, context_messages=50):
        self.bot = bot
        self.db = bot.db
        self.keep_messages = keep_messages
        self.keep_seconds = keep_seconds
        self.max_chars = max_chars
        self.context_messages = context_messages
        self.last_prune = 0
        self.db.execute('''CREATE TABLE IF NOT EXISTS chat_history(
          chat INTEGER, message_id INTEGER, uid INTEGER, name TEXT, text TEXT, at INTEGER, is_bot INTEGER DEFAULT 0,
          PRIMARY KEY(chat,message_id))''')
        columns = {row[1] for row in self.db.execute('PRAGMA table_info(chat_history)')}
        for column in ('reply_to_id', 'topic_id'):
            if column not in columns:
                self.db.execute(f'ALTER TABLE chat_history ADD COLUMN {column} INTEGER')
        self.db.execute('CREATE INDEX IF NOT EXISTS chat_history_recent ON chat_history(chat,at)')
        self.db.commit()

    def record(self, msg, is_bot=False):
        """Store one group message. Only the configured group is retained."""
        text = (msg.get('text') or msg.get('caption') or '').strip()
        chat = msg.get('chat', {}).get('id')
        if not text or chat != self.bot.group or not msg.get('message_id'):
            return
        if len(text) > self.max_chars:
            text = text[:self.max_chars] + '…'
        sender = msg.get('from') or msg.get('sender_chat') or {}
        if is_bot:
            name = '机器人'
        else:
            name = ' '.join(str(sender.get(k) or '') for k in ('first_name', 'last_name')).strip() \
                   or sender.get('title') or sender.get('username') or str(sender.get('id') or '')
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO chat_history '
                            '(chat,message_id,uid,name,text,at,is_bot,reply_to_id,topic_id) VALUES(?,?,?,?,?,?,?,?,?)',
                            (chat, int(msg['message_id']), int(sender.get('id') or 0), name[:64],
                             text, int(msg.get('date') or time.time()), int(is_bot),
                             msg.get('reply_to_message', {}).get('message_id'), msg.get('message_thread_id')))
        # Telegram supplies the immediate quote even when we missed its update.
        parent = msg.get('reply_to_message') or {}
        if (parent.get('message_id') and parent['message_id'] != msg['message_id']
                and parent.get('chat', {}).get('id', chat) == chat
                and not self.db.execute('SELECT 1 FROM chat_history WHERE chat=? AND message_id=?',
                                        (chat, parent['message_id'])).fetchone()):
            snapshot = dict(parent, chat={'id': chat})
            snapshot.pop('reply_to_message', None)
            self.record(snapshot, is_bot=1 if parent.get('from', {}).get('is_bot') else 0)

    def record_outgoing(self, chat, message_id, text, reply_to_id=None, topic_id=None, ai=False):
        """Store a message this bot posted, so follow-up questions keep their continuity."""
        plain = html.unescape(TAGS.sub('', text or '')).strip()
        if chat != self.bot.group or not message_id or not plain:
            return
        self.record({'chat': {'id': chat}, 'message_id': message_id, 'text': plain,
                     'from': {'id': self.bot.bot_id, 'first_name': '机器人'},
                     'reply_to_message': {'message_id': reply_to_id}, 'message_thread_id': topic_id},
                    is_bot=2 if ai or '\n模型 ID：' in plain else 1)

    def recent(self, chat, exclude_id=None, limit=40, max_chars=6000, seconds=21600, conversation_only=False, before_id=None, topic_id=None):
        """Newest-first selection, returned oldest-first and capped by characters."""
        cutoff = int(time.time()) - seconds
        rows = self.db.execute(
            'SELECT * FROM chat_history WHERE chat=? AND at>=? AND message_id!=? '
            'AND (? IS NULL OR message_id<?) AND (? IS NULL OR topic_id=?) ORDER BY at DESC, message_id DESC LIMIT ?',
            (chat, cutoff, int(exclude_id or -1), before_id, before_id, topic_id, topic_id, limit * 4 if conversation_only else limit)).fetchall()
        picked, used = [], 0
        for row in rows:
            if conversation_only and (row['is_bot'] == 1 or row['text'].lstrip().startswith('/')
                                      or row['text'].strip() in ('签到', '积分', '排行榜', '原神榜')):
                continue
            if len(picked) >= limit:
                break
            label = ('AI 助手' if row['is_bot'] == 2 else '机器人') if row['is_bot'] else (row['name'] or str(row['uid']))
            prefix = f'[消息 #{row["message_id"]}' + (f' 回复 #{row["reply_to_id"]}' if row['reply_to_id'] else '') + '] ' if conversation_only else ''
            line = f'{prefix}{label}: {row["text"]}'
            if used + len(line) > max_chars:
                break
            picked.append(line)
            used += len(line) + 1
        picked.reverse()
        return '\n'.join(picked)

    def reply_chain(self, msg, max_depth=12, max_chars=6000):
        """Follow only recorded parent links in this group; never guess missing history."""
        if msg.get('chat', {}).get('id') != self.bot.group:
            return ''
        quoted = msg.get('reply_to_message') or {}
        mid = quoted.get('message_id')
        seen, picked, used = set(), [], 0
        while mid and mid not in seen and len(picked) < max_depth:
            seen.add(mid)
            row = self.db.execute('SELECT * FROM chat_history WHERE chat=? AND message_id=?',
                                  (self.bot.group, mid)).fetchone()
            if not row:
                picked.append({'消息ID': mid, '说明': '此消息未保存，无法还原更早内容'})
                break
            # An old or malformed link must never pull in a later message.
            if msg.get('message_id') and mid >= msg['message_id']:
                break
            item = {'消息ID': mid, '回复消息ID': row['reply_to_id'],
                    '发言人': 'AI 助手' if row['is_bot'] == 2 else row['name'],
                    '内容': row['text']}
            size = len(json.dumps(item, ensure_ascii=False))
            if used + size > max_chars:
                break
            picked.append(item)
            used += size
            mid = row['reply_to_id']
        picked.reverse()
        return json.dumps(picked, ensure_ascii=False) if picked else ''

    def prune(self, now=None):
        """Keep the table bounded; runs at most once every five minutes."""
        now = int(now if now is not None else time.time())
        if now - self.last_prune < 300:
            return
        self.last_prune = now
        with self.db:
            self.db.execute('DELETE FROM chat_history WHERE at < ?', (now - self.keep_seconds,))
            for row in self.db.execute('SELECT DISTINCT chat FROM chat_history').fetchall():
                # Always keep the newest rows, drop everything older than the retained set.
                self.db.execute(
                    'DELETE FROM chat_history WHERE chat=? AND message_id NOT IN '
                    '(SELECT message_id FROM chat_history WHERE chat=? ORDER BY at DESC, message_id DESC LIMIT ?)',
                    (row[0], row[0], self.keep_messages))
