"""按 command_descriptions.json 刷新已有 Telegram 菜单说明；不发送任何群消息。

在生产主机运行：python3 /opt/tgbot/scripts/update_commands.py
命令处理函数仍然自己负责权限校验；本脚本只维护菜单文案。
"""
import json
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
# 导入 bot 之前就关掉字节码缓存，否则会在 /opt/tgbot 下留一个 __pycache__。
sys.dont_write_bytecode = True
from bot import config
from botapi import API


def main():
    descriptions = json.loads(Path(__file__).with_name('command_descriptions.json').read_text())
    cfg = config(str(BASE / '.env'))
    api = API(cfg['TELEGRAM_BOT_TOKEN'])
    group = int(cfg.get('GROUP_ID') or -1003014905211)

    def call(method, **kwargs):
        # 重试临时失败，不把凭据写进日志。
        for attempt in range(4):
            try:
                return api.call(method, **kwargs)
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)

    scopes = [{'type': name} for name in
              ('default', 'all_private_chats', 'all_group_chats', 'all_chat_administrators')]
    scopes.extend([{'type': 'chat', 'chat_id': group},
                   {'type': 'chat_administrators', 'chat_id': group}])
    for member in call('getChatAdministrators', chat_id=group):
        if not member['user'].get('is_bot'):
            scopes.append({'type': 'chat_member', 'chat_id': group,
                           'user_id': member['user']['id']})
    checked = updated = 0
    for scope in scopes:
        for language in ('', 'en', 'zh'):
            kwargs = {'scope': scope, 'language_code': language}
            old = call('getMyCommands', **kwargs)
            # 下线命令会从菜单移除；已有菜单补齐命令表中的新条目，空菜单保持继承。
            new = [{'command': item['command'], 'description': descriptions[item['command']]}
                   for item in old if item['command'] in descriptions]
            if old:
                existing = {item['command'] for item in new}
                new.extend({'command': name, 'description': description}
                           for name, description in descriptions.items() if name not in existing)
            checked += 1
            if new != old:
                call('setMyCommands', commands=new, **kwargs)
                if call('getMyCommands', **kwargs) != new:
                    raise RuntimeError(f'setMyCommands readback mismatch scope={scope} lang={language!r}')
                updated += 1
    print(f'Menu variants checked: {checked}; updated: {updated}')


if __name__ == '__main__':
    main()
