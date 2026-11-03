import unittest
from unittest.mock import patch
from scripts import update_commands


class MenuTests(unittest.TestCase):
    def test_single_group_menu_removes_retired_commands_and_retries_timeout(self):
        menus = {}
        calls = []
        first = [True]
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
                                   ('help', 'airdrop', 'airdrops', 'groups', 'group_add')]
                                  if data['scope']['type'] == 'default' and not data.get('language_code') else [])
                return menus[key]
            if method == 'setMyCommands':
                menus[key] = data['commands']
                return True
            raise AssertionError(method)
        with patch.object(update_commands, 'API') as api, patch.object(update_commands, 'config', return_value={'TELEGRAM_BOT_TOKEN': 'fake', 'GROUP_ID': '-1'}), patch.object(update_commands.time, 'sleep') as sleep, patch('builtins.print'):
            api.return_value.call.side_effect = api_call
            update_commands.main()
        sleep.assert_called_once_with(1)
        self.assertEqual({d['chat_id'] for m,d in calls if m == 'getChatAdministrators'}, {-1})
        names = {item['command'] for menu in menus.values() for item in menu}
        self.assertFalse(names.intersection({'airdrop','airdrops','groups','group_add',
                                             'ai','treasure','treasures','keyword','keywords'}))
        self.assertTrue({'rate','invite'} <= names)
        self.assertTrue(any(menu == [] for menu in menus.values()))

    def test_repeated_service_failure_stops_without_writing_menus(self):
        with patch.object(update_commands, 'API') as api, patch.object(update_commands, 'config', return_value={'TELEGRAM_BOT_TOKEN': 'fake','GROUP_ID': '-1'}), patch.object(update_commands.time, 'sleep') as sleep:
            api.return_value.call.side_effect = TimeoutError('unavailable')
            with self.assertRaises(TimeoutError):update_commands.main()
            self.assertEqual(api.return_value.call.call_count,4)
            self.assertEqual(sleep.call_count,3)
            self.assertTrue(all(call.args[0] == 'getChatAdministrators' for call in api.return_value.call.call_args_list))
