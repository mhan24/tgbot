import io
import json
import sqlite3
import unittest
from unittest.mock import patch
from bot import Bot
from ai_chat import completion_result, AIError
from vision import download_images, ImageError
from test_bot import FakeAPI
from test_ai_chat import Pool


class VisionTests(unittest.TestCase):
    def setUp(self):
        self.bot = Bot({'GROUP_ID': '-1', 'CHANNEL_ID': '-2', 'AI_BASE_URL': 'https://example.test/v1',
                        'AI_API_KEY': 'key', 'AI_MODEL': 'grok-4.7', 'TELEGRAM_BOT_TOKEN': 'secret'},
                       FakeAPI(), sqlite3.connect(':memory:'))
        self.media = self.bot.ai.media

    def photo(self, mid, album='a', chat=-1):
        return {'message_id': mid, 'chat': {'id': chat, 'type': 'supergroup'}, 'media_group_id': album,
                'from': {'id': 2}, 'photo': [{'file_id': f'small{mid}', 'width': 20, 'height': 20},
                                           {'file_id': f'large{mid}', 'width': 600, 'height': 800}]}

    def test_album_order_dedup_and_chat_isolation(self):
        for mid in (3, 1, 2):self.media.observe(self.photo(mid))
        self.media.observe(self.photo(4, chat=-999))
        msg = {'chat': {'id': -1}, 'reply_to_message': self.photo(2)}
        files, _ = self.media.collect(msg)
        self.assertEqual(files, ['large1', 'large2', 'large3'])

    def test_old_unseen_quote_single_image_still_available(self):
        files, _ = self.media.collect({'chat': {'id': -1}, 'reply_to_message': self.photo(50)})
        self.assertEqual(files, ['large50'])

    def test_image_download_failure_does_not_call_model_or_expose_token(self):
        with patch('vision.urllib.request.urlopen', side_effect=RuntimeError('secret URL')):
            with self.assertRaises(AIError) as error:
                completion_result(self.bot.cfg, 'question', ['file'])
        self.assertNotIn('secret', str(error.exception))

    def test_telegram_image_transferred_as_data_not_token_url(self):
        png = b'\x89PNG\r\n\x1a\nTEST'
        metadata = json.dumps({'ok': True, 'result': {'file_path': 'photos/test.png'}}).encode()
        with patch('vision.urllib.request.urlopen', side_effect=[io.BytesIO(metadata), io.BytesIO(png)]):
            parts = download_images(self.bot.cfg, ['file'])
        self.assertTrue(parts[0]['image_url']['url'].startswith('data:image/png;base64,'))
        self.assertNotIn('secret', str(parts))
        with patch('ai_chat.download_images', return_value=parts), patch('ai_chat.urllib.request.urlopen',
                    return_value=io.BytesIO(b'{"model":"grok-4.7","choices":[{"message":{"content":"image answer"}}]}')) as call:
            self.assertEqual(completion_result(self.bot.cfg, 'question', ['file'])[0], 'image answer')
            payload = json.loads(call.call_args.args[0].data)
            self.assertEqual(payload['messages'][-1]['content'][1]['type'], 'image_url')

    def test_photo_without_caption_is_valid_ai_input(self):
        class ImagePool(Pool):
            def submit(pool, fn, cfg, prompt, *images):
                pool.images = images
                return super(ImagePool, pool).submit(fn, cfg, prompt)
        pool = ImagePool();self.bot.ai.pool = pool
        msg = {'chat': {'id': -1}, 'message_id': 5, 'from': {'id': 1}, 'text': '/ai',
               'reply_to_message': self.photo(4)}
        self.bot.ai.command(msg)
        self.assertEqual(pool.images, (['large4'],))
        self.assertIn('实际图片', pool.prompts[0])

    def test_image_document_supported_and_expired_album_ignored(self):
        msg = {'chat': {'id': -1}, 'message_id': 4, 'document': {'mime_type': 'image/png', 'file_id': 'doc'}}
        self.assertEqual(self.media.collect({'chat': {'id': -1}, 'reply_to_message': msg})[0], ['doc'])
        self.media.observe(self.photo(1))
        self.bot.db.execute('UPDATE ai_media SET at=0')
        self.assertEqual(self.media.collect({'chat': {'id': -1}, 'reply_to_message': self.photo(2)})[0], ['large2'])

    def test_oversized_images_fail_explicitly(self):
        metadata = json.dumps({'ok': True, 'result': {'file_path': 'x', 'file_size': 100000000}}).encode()
        with patch('vision.urllib.request.urlopen', return_value=io.BytesIO(metadata)):
            with self.assertRaises(ImageError):download_images(self.bot.cfg, ['file'])
