"""Refresh descriptions in existing Telegram menus; never send chat messages.

Run on the production host: python3 /opt/tgbot/scripts/update_commands.py
Command handlers remain responsible for checking permissions.
"""
import json
import sqlite3
import sys
import time
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE))
from bot import API, config


def main():
    descriptions = json.loads(Path(__file__).with_name('command_descriptions.json').read_text())
    api = API(config(str(BASE / '.env'))['TELEGRAM_BOT_TOKEN'])

    def call(method, **kwargs):
        # Retry transient API failures without exposing credentials in logs.
        for attempt in range(4):
            try:
                return api.call(method, **kwargs)
            except Exception:
                if attempt == 3:
                    raise
                time.sleep(attempt + 1)

    db = sqlite3.connect('/var/lib/tgbot/bot.sqlite3')
    scopes = [{'type': name} for name in
              ('default', 'all_private_chats', 'all_group_chats', 'all_chat_administrators')]
    for (gid,) in db.execute('SELECT chat FROM managed_groups'):
        scopes.extend([{'type': 'chat', 'chat_id': gid},
                       {'type': 'chat_administrators', 'chat_id': gid}])
        for member in call('getChatAdministrators', chat_id=gid):
            if not member['user'].get('is_bot'):
                scopes.append({'type': 'chat_member', 'chat_id': gid, 'user_id': member['user']['id']})
    checked = updated = 0
    for scope in scopes:
        for language in ('', 'en', 'zh'):
            kwargs = {'scope': scope, 'language_code': language}
            old = call('getMyCommands', **kwargs)
            # Preserve empty scopes so Telegram continues to inherit its fallback menu.
            new = [{'command': c['command'], 'description': descriptions[c['command']]}
                   for c in old if c['command'] in descriptions]
            retired = {'warn', 'warnings', 'resetwarn', 'mute', 'unmute', 'ban', 'unban', 'white', 'unwhite', 'whitelist'}
            if any(c['command'] in retired for c in old) and not any(c['command'] == 'manage' for c in new):
                new.append({'command': 'manage', 'description': descriptions['manage']})
            checked += 1
            if new != old:
                call('setMyCommands', commands=new, **kwargs)
                assert call('getMyCommands', **kwargs) == new
                updated += 1
    print(f'Menu variants checked: {checked}; updated: {updated}')


if __name__ == '__main__':
    main()
