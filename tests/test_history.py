import sqlite3
import time
import unittest
from unittest.mock import patch
from bot import Bot
from ai_chat import AIChat
from test_bot import FakeAPI

class Tests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'AI_BASE_URL': 'https://x.test/v1',
                        'AI_API_KEY': 'k', 'AI_MODEL': 'm'}, self.api, sqlite3.connect(':memory:'))
        self.bot.bot_id, self.bot.username = 99, 'testbot'
        self.bot.can_manage_tags = True
        self.h = self.bot.history
        self.group = self.bot.group

    def msg(self, mid, text, uid=2, name='Alice', date=None):
        return {'chat': {'id': self.group, 'type': 'supergroup'}, 'message_id': mid,
                'from': {'id': uid, 'first_name': name, 'is_bot': False},
                'text': text, 'date': date or int(time.time())}

    def test_group_messages_are_recorded_with_names(self):
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '今天天气不错')})
        row = self.bot.db.execute('SELECT * FROM chat_history WHERE message_id=1').fetchone()
        self.assertEqual(row['name'], 'Alice')
        self.assertEqual(row['text'], '今天天气不错')
        self.assertEqual(row['is_bot'], 0)

    def test_private_and_other_chats_are_not_recorded(self):
        self.bot.handle({'update_id': 1, 'message': {'chat': {'id': 5, 'type': 'private'}, 'message_id': 1,
                        'from': {'id': 2}, 'text': 'secret'}})
        self.bot.handle({'update_id': 2, 'message': {'chat': {'id': -555, 'type': 'supergroup'}, 'message_id': 2,
                        'from': {'id': 2}, 'text': 'elsewhere'}})
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM chat_history').fetchone()[0], 0)

    def test_bot_replies_are_recorded(self):
        self.bot.send(self.group, 'AI 的回答')
        row = self.bot.db.execute('SELECT * FROM chat_history ORDER BY message_id DESC').fetchone()
        self.assertEqual(row['is_bot'], 1)
        self.assertEqual(row['text'], 'AI 的回答')

    def prompt(self):
        return self.bot.db.execute('SELECT prompt FROM ai_jobs').fetchone()['prompt']

    def test_context_reaches_the_prompt(self):
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '我们说的是比特币空投')})
        self.bot.handle({'update_id': 2, 'message': self.msg(2, '/ai 那是什么？')})
        prompt = self.prompt()
        self.assertIn('最近的群聊记录', prompt)
        self.assertIn('A' if False else 'Alice', prompt)
        self.assertIn('我们说的是比特币空投', prompt)
        self.assertIn('那是什么？', prompt)
        # the trigger itself is the request, not part of the quoted history
        self.assertIn('Alice: 我们说的是比特币空投', prompt)

    def test_trigger_message_is_not_duplicated_in_context(self):
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '/ai 你好')})
        self.assertEqual(self.prompt().count('你好'), 1)

    def test_private_trigger_has_no_group_context(self):
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '群里的秘密')})
        self.bot.handle({'update_id': 2, 'message': {'chat': {'id': 5, 'type': 'private'}, 'message_id': 9,
                        'from': {'id': 2, 'first_name': 'Alice'}, 'text': '/ai 你好'}})
        job = self.bot.db.execute('SELECT prompt FROM ai_jobs').fetchone()
        self.assertNotIn('群里的秘密', job['prompt'])

    def test_unreported_message_is_stored(self):
        self.bot.handle({'update_id': 1, 'message': self.msg(1, '日赚1000，私聊我')})
        users = self.bot.db.execute('SELECT count(*) FROM chat_history WHERE is_bot=0').fetchone()[0]
        self.assertEqual(users, 1)

    def test_default_context_is_fifty_messages(self):
        for i in range(1, 80):
            self.h.record(self.msg(i, f'消息{i}'))
        self.assertEqual(self.h.keep_messages, 50)
        self.assertEqual(self.h.context_messages, 50)
        context = self.bot.ai_context(self.msg(200, '/ai 上面说了什么'))
        self.assertIn('消息79', context)
        self.assertNotIn('消息20', context)
        self.assertEqual(len(context.splitlines()), 50)

    def test_context_is_character_capped(self):
        for i in range(1, 60):
            self.h.record(self.msg(i, 'x' * 300, uid=100 + i, name=f'U{i}'))
        context = self.h.recent(self.group, limit=self.h.context_messages, max_chars=6000)
        self.assertLessEqual(len(context), 6300)

    def test_prune_keeps_newest_messages(self):
        for i in range(1, 30):
            self.h.record(self.msg(i, f'msg{i}', date=int(time.time()) + i))
        self.h.keep_messages = 5
        self.h.prune(now=int(time.time()) + 1000)
        kept = [r['message_id'] for r in self.bot.db.execute('SELECT message_id FROM chat_history ORDER BY message_id')]
        self.assertEqual(kept, [25, 26, 27, 28, 29])

    def test_prune_drops_old_messages(self):
        self.h.record(self.msg(1, 'old', date=int(time.time()) - 200000))
        self.h.record(self.msg(2, 'new'))
        self.h.prune(now=int(time.time()))
        self.assertEqual([r['message_id'] for r in self.bot.db.execute('SELECT message_id FROM chat_history')], [2])

    def test_long_message_is_truncated(self):
        self.h.record(self.msg(1, 'a' * 5000))
        text = self.bot.db.execute('SELECT text FROM chat_history').fetchone()[0]
        self.assertLessEqual(len(text), 1001)

    def test_history_failure_does_not_break_the_trigger(self):
        self.bot.handle({'update_id': 1, 'message': self.msg(1, 'hello')})
        with patch.object(self.bot, 'ai_context', side_effect=RuntimeError('boom')):
            pass
        with patch.object(self.bot.history, 'recent', side_effect=RuntimeError('boom')):
            self.assertFalse(self.bot.ai_context(self.msg(2, 'hi')))

    def test_quote_keeps_group_background_and_current_question(self):
        self.h.record(self.msg(1, '完全无关的空投话题'))
        message = self.msg(2, '/ai 总结')
        message['reply_to_message'] = {'text': '需要总结的新闻'}
        self.bot.handle({'update_id': 2, 'message': message})
        self.assertIn('完全无关的空投话题', self.prompt())
        self.assertIn('需要总结的新闻', self.prompt())
        self.assertEqual(self.prompt().count('当前用户问题'), 1)

    def test_context_excludes_commands_notifications_and_future_messages(self):
        self.h.record(self.msg(1, '正常讨论'))
        self.h.record(self.msg(2, '/ai 旧问题'))
        self.h.record(self.msg(3, '签到'))
        self.h.record(self.msg(4, '管理通知'), is_bot=True)
        self.h.record(self.msg(20, '之后的消息'))
        context = self.bot.ai_context(self.msg(10, '/ai 解释'))
        self.assertIn('正常讨论', context)
        for excluded in ('旧问题', '签到', '管理通知', '之后的消息'):
            self.assertNotIn(excluded, context)

    def test_empty_ai_with_history_still_requires_question(self):
        self.h.record(self.msg(1, '正常讨论'))
        self.bot.handle({'update_id': 2, 'message': self.msg(2, '/ai')})
        self.assertIsNone(self.bot.db.execute('SELECT prompt FROM ai_jobs').fetchone())

    def test_ai_answers_retained_but_bot_notifications_filtered(self):
        self.h.record_outgoing(self.group, 1, '机器人通知')
        self.h.record_outgoing(self.group, 2, '巴黎是法国首都。\n模型 ID：grok-4.7')
        context = self.bot.ai_context(self.msg(3, '/ai 继续解释'))
        self.assertIn('巴黎是法国首都', context)
        self.assertNotIn('机器人通知', context)

    def test_reply_chain_follows_parents_not_interleaved_topic(self):
        import json
        first = self.msg(1, '周日去苏州博物馆')
        reply = self.msg(3, '那就这么定了')
        reply['reply_to_message'] = first
        self.h.record(first)
        self.h.record(self.msg(2, '另一个话题：比特币'))
        self.h.record(reply)
        trigger = self.msg(4, '/ai 解释约定')
        trigger['reply_to_message'] = reply
        self.bot.handle({'update_id': 4, 'message': trigger})
        material = json.loads(self.prompt().split('：\n', 1)[1].split('\n\n当前用户问题')[0])
        chain = material['引用对话链（按先后顺序，优先用于理解引用）']
        self.assertEqual([item['消息ID'] for item in chain], [1, 3])
        self.assertEqual(chain[-1]['回复消息ID'], 1)
        self.assertNotIn('比特币', str(chain))

    def test_ai_output_links_to_original_question(self):
        self.h.record(self.msg(1, '/ai 为什么'))
        self.bot.call('sendMessage', chat_id=self.group, text='回答 &amp; 说明',
                      reply_parameters={'message_id': 1}, _ai_history=True)
        row = self.bot.db.execute('SELECT * FROM chat_history WHERE is_bot=2').fetchone()
        self.assertEqual(row['reply_to_id'], 1)
        self.assertEqual(row['text'], '回答 & 说明')
        self.assertFalse(any('_ai_history' in data for method, data in self.api.calls))

    def test_history_schema_upgrade_preserves_existing_rows(self):
        from history import History
        db = sqlite3.connect(':memory:')
        db.row_factory = sqlite3.Row
        db.execute('CREATE TABLE chat_history(chat INTEGER,message_id INTEGER,uid INTEGER,name TEXT,text TEXT,at INTEGER,is_bot INTEGER,PRIMARY KEY(chat,message_id))')
        db.execute('INSERT INTO chat_history VALUES(-1,1,2,"A","old",1,0)')
        from types import SimpleNamespace
        history = History(SimpleNamespace(db=db, group=-1))
        row = db.execute('SELECT * FROM chat_history').fetchone()
        self.assertEqual(row['text'], 'old')
        self.assertIsNone(row['reply_to_id'])
        history.record(self.msg(2, 'new'))
        self.assertEqual(db.execute('SELECT count(*) FROM chat_history').fetchone()[0], 2)

    def test_chain_is_group_scoped_and_missing_parent_is_explicit(self):
        import json
        reply = self.msg(2, '回答')
        reply['reply_to_message'] = {'message_id': 1}
        self.h.record(reply)
        trigger = self.msg(3, '/ai 解释')
        trigger['reply_to_message'] = reply
        chain = json.loads(self.h.reply_chain(trigger))
        self.assertIn('未保存', chain[0]['说明'])
        trigger['chat']['id'] = -999
        self.assertEqual(self.h.reply_chain(trigger), '')

    def test_regular_reply_thread_keeps_group_context(self):
        self.h.record(self.msg(1, '先前讨论的重要背景'))
        quoted = self.msg(2, '这个怎么样')
        quoted['message_thread_id'] = 2
        self.h.record(quoted)
        trigger = self.msg(3, '/ai 结合上下文解释')
        trigger['message_thread_id'] = 2
        self.assertIn('先前讨论的重要背景', self.bot.ai_context(trigger))

    def test_forum_context_does_not_mix_topics(self):
        first = self.msg(1, '话题一');first['message_thread_id'] = 10;first['is_topic_message'] = True
        second = self.msg(2, '话题二');second['message_thread_id'] = 20;second['is_topic_message'] = True
        self.h.record(first);self.h.record(second)
        trigger = self.msg(3, '/ai 解释');trigger['message_thread_id'] = 10;trigger['is_topic_message'] = True
        context = self.bot.ai_context(trigger)
        self.assertIn('话题一', context)
        self.assertNotIn('话题二', context)


if __name__ == '__main__':
    unittest.main()
