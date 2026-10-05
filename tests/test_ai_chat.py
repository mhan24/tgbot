import io
import json
import sqlite3
import unittest
from concurrent.futures import Future
from unittest.mock import patch
from bot import Bot
from ai_chat import completion,completion_result,AIError,AIChat,telegram_html,split_html
from test_bot import FakeAPI

class Pool:
    def __init__(self):self.prompts=[]
    def submit(self,fn,cfg,prompt):
        self.prompts.append(prompt);f=Future();f.set_result(('测试回答 <b>不是HTML</b>','deepseek-v4-flash'));return f

class Tests(unittest.TestCase):
    def setUp(self):
        self.api=FakeAPI();self.bot=Bot({'GROUP_ID':'-1','CHANNEL_ID':'-2','AI_BASE_URL':'https://example.test/v1','AI_API_KEY':'test-key','AI_MODEL':'deepseek-v4-flash'},self.api,sqlite3.connect(':memory:'))
        self.bot.username='testbot';self.pool=Pool();self.bot.ai.pool=self.pool
    def msg(self,mid=1,uid=2,text='/ai 你好',chat=-1):
        return {'chat':{'id':chat,'type':'private' if chat>0 else 'supergroup'},'from':{'id':uid},'message_id':mid,'text':text}
    def test_model_payload(self):
        with patch('ai_chat.urllib.request.urlopen',return_value=io.BytesIO(b'{"choices":[{"message":{"content":"ok"}}]}')) as request:
            self.assertEqual(completion(self.bot.cfg,'hi'),'ok')
            payload=json.loads(request.call_args.args[0].data)
            self.assertEqual(payload['model'],'deepseek-v4-flash')
            self.assertEqual(payload['messages'][-1]['content'],'hi')

    def test_completion_records_model_id_returned_by_provider(self):
        body=b'{"model":"provider/deepseek-v4-flash-2026-09","choices":[{"message":{"content":"ok"}}]}'
        with patch('ai_chat.urllib.request.urlopen',return_value=io.BytesIO(body)):
            self.assertEqual(completion_result(self.bot.cfg,'hi'),
                             ('ok','provider/deepseek-v4-flash-2026-09'))
    def test_regular_cooldown_shared_between_private_and_group(self):
        with patch('ai_chat.time.time',return_value=100):self.bot.ai.command(self.msg())
        with patch('ai_chat.time.time',return_value=159):self.bot.ai.command(self.msg(2,chat=2))
        self.assertEqual(len(self.pool.prompts),1)
        with patch('ai_chat.time.time',return_value=160):self.bot.ai.command(self.msg(3))
        self.assertEqual(len(self.pool.prompts),2)
    def test_admin_unlimited_and_replay_dedup(self):
        for i in range(5):self.bot.ai.command(self.msg(i,uid=1))
        self.bot.ai.command(self.msg(4,uid=1))
        self.assertEqual(len(self.pool.prompts),5)
    def test_quoted_text_and_optional_instruction(self):
        msg=self.msg(text='/ai 总结');msg['reply_to_message']={'text':'引用内容'}
        self.bot.ai.command(msg)
        self.assertIn('引用内容',self.pool.prompts[0]);self.assertIn('总结',self.pool.prompts[0])
    def test_only_canonical_ai_command_is_supported(self):
        self.bot.ai.command(self.msg(uid=1,text='/ai 直接回答'))
        self.bot.ai.command(self.msg(2,uid=1,text='/ai@testbot 继续回答'))
        self.assertFalse(self.bot.ai.command(self.msg(3,uid=1,text='原神启动 旧触发词')))
        self.assertEqual(len(self.pool.prompts),2)
        self.assertIn('直接回答',self.pool.prompts[0])
        self.assertIn('继续回答',self.pool.prompts[1])
    def test_english_ai_command_counts_as_group_operation(self):
        self.assertTrue(self.bot.is_bot_operation(self.msg(text='/ai 你好')))
        self.assertFalse(self.bot.is_bot_operation(self.msg(text='/ai@otherbot 你好')))
    def test_quote_only_caption(self):
        msg=self.msg(text='/ai');msg['reply_to_message']={'caption':'图片说明'}
        self.bot.ai.command(msg)
        self.assertIn('图片说明',self.pool.prompts[0])
    def test_no_prompt_does_not_use_quota(self):
        self.bot.ai.command(self.msg(text='/ai'))
        self.assertEqual(self.bot.db.execute('SELECT count(*) FROM ai_cooldown').fetchone()[0],0)
    def test_reply_html_and_split(self):
        self.bot.ai.command(self.msg());self.bot.ai.drain()
        sent=[d for m,d in self.api.calls if m=='sendMessage']
        self.assertEqual(sent[-1]['text'],'测试回答 &lt;b&gt;不是HTML&lt;/b&gt;\n模型 ID：deepseek-v4-flash')
        self.assertEqual(sent[-1]['parse_mode'],'HTML')
        self.assertEqual(self.bot.db.execute('SELECT status FROM ai_jobs').fetchone()[0],'sent')
    def test_markdown_to_telegram_html(self):
        text=telegram_html('# 标题\n\n这是 **粗体** 和 *斜体*，还有 `code`。\n\n- 一项\n- 二项\n\n1. 先\n2. 后\n\n> 引用\n\n[链接](https://example.com)\n\n```python\nprint("<hi>")\n```\n\n普通 <html> 应转义')
        self.assertIn('<b>标题</b>',text)
        self.assertIn('<b>粗体</b>',text)
        self.assertIn('<i>斜体</i>',text)
        self.assertIn('<code>code</code>',text)
        self.assertIn('• 一项',text)
        self.assertIn('1. 先',text)
        self.assertIn('<blockquote>引用</blockquote>',text)
        self.assertIn('<a href="https://example.com">链接</a>',text)
        self.assertIn('<pre><code class="language-python">print(&quot;&lt;hi&gt;&quot;)</code></pre>',text)
        self.assertIn('&lt;html&gt;',text)
        self.assertNotIn('<html>',text)
    def test_html_split_keeps_tags(self):
        chunks=split_html('<b>'+'字'*80+'</b>',limit=40)
        self.assertGreater(len(chunks),1)
        self.assertTrue(all(c.startswith('<b>') and c.endswith('</b>') for c in chunks))
    def test_restart_preserves_cooldown_no_reissue(self):
        self.bot.ai.command(self.msg())
        other=AIChat(self.bot);other.pool=Pool();other.command(self.msg(2))
        self.assertEqual(other.pool.prompts,[])
        self.assertEqual(self.bot.db.execute('SELECT status FROM ai_jobs').fetchone()[0],'done')
    def test_nonmember_and_unverified_rejected(self):
        self.api.members[2]={'status':'left'};self.bot.ai.command(self.msg())
        self.assertFalse(self.pool.prompts)
        self.api.members[2]={'status':'member'}
        with self.bot.db:self.bot.db.execute("INSERT INTO verification(uid,kind) VALUES(2,'member')")
        self.bot.ai.command(self.msg());self.assertFalse(self.pool.prompts)
    def test_errors_do_not_expose_key(self):
        with patch('ai_chat.urllib.request.urlopen',side_effect=TimeoutError('private token')):
            with self.assertRaises(AIError) as exc:completion(self.bot.cfg,'hi')
            self.assertNotIn('private token',str(exc.exception))
    def test_dispatch_for_group_and_private(self):
        self.bot.handle({'update_id':1,'message':self.msg(uid=1)})
        self.bot.handle({'update_id':2,'message':self.msg(2,uid=1,chat=1)})
        self.assertEqual(len(self.pool.prompts),2)
