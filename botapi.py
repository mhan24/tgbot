# coding: utf-8
"""Telegram Bot API 传输层；只依赖标准库，不包含业务判断。"""
import json
import urllib.error
import urllib.request

DEFAULT_TIMEOUT = 45


class APIError(Exception):
    """一次 Telegram 调用失败；区分可重试失败与永久失败。"""

    def __init__(self, method, data):
        self.method = method
        self.code = data.get('error_code', 0)
        self.description = str(data.get('description') or 'Network failure')
        try:
            self.retry_after = max(1, int(data.get('parameters', {}).get('retry_after', 5)))
        except (TypeError, ValueError, AttributeError):
            self.retry_after = 5
        super().__init__(f'{method}: {self.code} {self.description}')

    @property
    def permanent(self):
        """400/403 通常是参数、权限或用户状态问题，重试没有意义。"""
        return self.code in (400, 403)


class API:
    def __init__(self, token, timeout=DEFAULT_TIMEOUT):
        self.base = 'https://api.telegram.org/bot' + token + '/'
        self.file_base = 'https://api.telegram.org/file/bot' + token + '/'
        self.timeout = timeout

    def call(self, method, **data):
        request = urllib.request.Request(self.base + method, data=json.dumps(data).encode(),
                                         headers={'Content-Type': 'application/json'})
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                payload = json.load(response)
        except urllib.error.HTTPError as exc:
            try:
                payload = json.load(exc)
            except Exception:
                payload = {'error_code': exc.code, 'description': 'HTTP error'}
        except (OSError, urllib.error.URLError):
            raise APIError(method, {'error_code': 503, 'description': 'Network failure'}) from None
        except (ValueError, UnicodeError):
            raise APIError(method, {'error_code': 503, 'description': 'Invalid API response'}) from None
        if not isinstance(payload, dict):
            raise APIError(method, {'error_code': 503, 'description': 'Invalid API response'})
        if not payload.get('ok'):
            raise APIError(method, payload if isinstance(payload.get('description'), str)
                           else {'error_code': payload.get('error_code', 503),
                                 'description': 'Invalid API response'})
        if 'result' not in payload:
            raise APIError(method, {'error_code': 503, 'description': 'Missing API result'})
        return payload['result']

    def download(self, file_path):
        """下载 getFile 给出的文件路径，返回 bytes；失败抛 APIError（/ai 图片识别用）。"""
        request = urllib.request.Request(self.file_base + file_path)
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise APIError('download', {'error_code': exc.code,
                                        'description': 'File download failed'}) from None
        except (OSError, urllib.error.URLError):
            raise APIError('download', {'error_code': 503, 'description': 'Network failure'}) from None
