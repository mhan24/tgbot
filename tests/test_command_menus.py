# coding: utf-8
"""命令菜单维护脚本：只同步命令表内的说明，下线命令从菜单移除。"""
import unittest
from unittest.mock import patch

from scripts import update_commands


class MenuTests(unittest.TestCase):
    def test_retired_commands_are_removed_and_timeout_is_retried(self):
        menus, calls, first = {}, [], [True]

        def api_call(method, **data):
            calls.append((method, data))
            if first[0]:
                first[0] = False
                raise TimeoutError('temporary')
            if method == 'getChatAdministrators':
                return []
            key = (tuple(sorted(data['scope'].items())), data.get('language_code', ''))
            if method == 'getMyCommands':
                if key not in menus:
                    menus[key] = ([{'command': name, 'description': 'old'} for name in
                                   ('help', 'checkin', 'points', 'report', 'manage')]
                                  if data['scope']['type'] == 'default'
                                  and not data.get('language_code') else [])
                return menus[key]
            if method == 'setMyCommands':
                menus[key] = data['commands']
                return True
            raise AssertionError(method)

        with patch.object(update_commands, 'API') as api, \
                patch.object(update_commands, 'config',
                             return_value={'TELEGRAM_BOT_TOKEN': 'fake', 'GROUP_ID': '-1'}), \
                patch.object(update_commands.time, 'sleep') as sleep, \
                patch('builtins.print'):
            api.return_value.call.side_effect = api_call
            update_commands.main()

        sleep.assert_called_once_with(1)
        self.assertEqual({data['chat_id'] for method, data in calls
                          if method == 'getChatAdministrators'}, {-1})
        commands = {item['command'] for menu in menus.values() for item in menu}
        self.assertEqual(commands, {'start', 'help', 'rate', 'bin'})
        self.assertFalse(commands & {'checkin', 'points', 'report', 'manage'})
        self.assertTrue(any(menu == [] for menu in menus.values()))

    def test_repeated_failure_stops_without_writing_menus(self):
        with patch.object(update_commands, 'API') as api, \
                patch.object(update_commands, 'config',
                             return_value={'TELEGRAM_BOT_TOKEN': 'fake', 'GROUP_ID': '-1'}), \
                patch.object(update_commands.time, 'sleep') as sleep:
            api.return_value.call.side_effect = TimeoutError('unavailable')
            with self.assertRaises(TimeoutError):
                update_commands.main()
            self.assertEqual(api.return_value.call.call_count, 4)
            self.assertEqual(sleep.call_count, 3)
            self.assertTrue(all(call.args[0] == 'getChatAdministrators'
                                for call in api.return_value.call.call_args_list))


if __name__ == '__main__':
    unittest.main()
