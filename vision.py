"""Telegram image references and bounded, in-memory downloads for AI requests."""
import base64
import json
import time
import urllib.request

MAX_IMAGES = 10
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024


class ImageError(Exception):
    pass


def image_file(msg):
    photos = msg.get('photo') or []
    if photos:
        return max(photos, key=lambda p: p.get('width', 0) * p.get('height', 0)).get('file_id')
    doc = msg.get('document') or {}
    if doc.get('mime_type') in ('image/jpeg', 'image/png', 'image/webp'):
        return doc.get('file_id')
    return None


class Media:
    def __init__(self, bot):
        self.bot = bot
        self.db = bot.db
        self.db.execute('''CREATE TABLE IF NOT EXISTS ai_media(
          chat INTEGER,message_id INTEGER,album TEXT,file_id TEXT,caption TEXT,at INTEGER,
          PRIMARY KEY(chat,message_id))''')
        self.db.execute('CREATE INDEX IF NOT EXISTS ai_media_album ON ai_media(chat,album)')
        self.db.commit()

    def observe(self, msg):
        chat = msg.get('chat', {})
        if chat.get('id') != self.bot.group and chat.get('type') != 'private':
            return
        fid = image_file(msg)
        if not fid or not msg.get('message_id'):
            return
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO ai_media VALUES(?,?,?,?,?,?)',
                            (chat['id'], msg['message_id'], msg.get('media_group_id'), fid,
                             (msg.get('caption') or '')[:1000], int(time.time())))
            self.db.execute('DELETE FROM ai_media WHERE at<?', (int(time.time()) - 86400,))
            self.db.execute('DELETE FROM ai_media WHERE chat=? AND message_id NOT IN '
                            '(SELECT message_id FROM ai_media WHERE chat=? ORDER BY at DESC,message_id DESC LIMIT 200)',
                            (chat['id'], chat['id']))

    def collect(self, msg):
        """Only current/quoted media and its same-chat album, never unrelated images."""
        files, captions = [], []
        chat = msg['chat']['id']
        for source in (msg.get('reply_to_message') or {}, msg):
            if source.get('chat', {}).get('id', chat) != chat:
                continue
            fid = image_file(source)
            album = source.get('media_group_id')
            rows = self.db.execute('SELECT file_id,caption FROM ai_media WHERE chat=? AND album=? '
                                   'AND at>=? ORDER BY message_id',
                                   (chat, album, int(time.time()) - 86400)).fetchall() if album else []
            for row in rows:
                if row['file_id'] not in files:
                    files.append(row['file_id'])
                if row['caption'] and row['caption'] not in captions:
                    captions.append(row['caption'])
            if fid and fid not in files:
                files.append(fid)
        if len(files) > MAX_IMAGES:
            raise ImageError(f'一次最多分析 {MAX_IMAGES} 张图片，请拆分后重试。')
        return files, captions


def download_images(cfg, file_ids):
    """Runs on an HTTP worker. Telegram token URLs never reach the AI provider."""
    token = cfg.get('TELEGRAM_BOT_TOKEN')
    if not token:
        raise ImageError('图片读取尚未配置，请联系管理员。')
    parts, total = [], 0
    try:
        for fid in file_ids:
            req = urllib.request.Request('https://api.telegram.org/bot' + token + '/getFile',
                                         data=json.dumps({'file_id': fid}).encode(),
                                         headers={'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=30) as response:
                meta = json.load(response)
            info = meta.get('result') or {}
            path = info.get('file_path')
            if not meta.get('ok') or not path:
                raise ImageError('无法读取引用图片，请重新发送图片后重试。')
            if info.get('file_size', 0) > MAX_FILE_BYTES:
                raise ImageError('单张图片超过 10 MB，请压缩后重试。')
            # File path originates from Telegram; reject unexpected absolute/escaping paths.
            if path.startswith('/') or any(p in ('.', '..') for p in path.split('/')) or ':' in path:
                raise ImageError('图片路径异常，请重新发送图片。')
            with urllib.request.urlopen('https://api.telegram.org/file/bot' + token + '/' + path, timeout=30) as response:
                data = response.read(MAX_FILE_BYTES + 1)
            total += len(data)
            if len(data) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise ImageError('图片大小超出限制，请减少图片或压缩后重试。')
            if data.startswith(b'\xff\xd8\xff'):
                mime = 'image/jpeg'
            elif data.startswith(b'\x89PNG\r\n\x1a\n'):
                mime = 'image/png'
            elif data.startswith(b'RIFF') and data[8:12] == b'WEBP':
                mime = 'image/webp'
            else:
                raise ImageError('图片格式不支持，请发送 JPG、PNG 或 WebP。')
            parts.append({'type': 'image_url', 'image_url': {
                'url': 'data:' + mime + ';base64,' + base64.b64encode(data).decode(), 'detail': 'high'}})
    except ImageError:
        raise
    except Exception:
        # Do not leak token-bearing request URLs through exception messages.
        raise ImageError('图片下载失败，未进行文字替代分析；请稍后重试。') from None
    return parts
