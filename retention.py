# coding: utf-8
"""Reading time for short notices; interactive cards use explicit lifetimes."""
import html
import re

NOTICE_SECONDS = 120
# Match notice wording, not a general occurrence of words such as 'warning' in /help.
FAILURE = re.compile(r'^(?:[⚠🚫❌]|用法[：:]|无法|暂时无法|不能|仅.{0,16}(?:可以|可|能)|请先|请回复|请使用|请直接|输入不|不支持|积分不足|已签到|今天已|今日已|签到失败|签到重复|重复签到)|(?:失败|未执行操作|未作处罚|不符合要求|请稍后重试|请重新|必须是|请输入|尚未完成|已过期|已超时|已经签到|已签过|已被拉黑|封禁并移出|已禁言用户)')


def notice_delay(text, default):
    plain = html.unescape(re.sub(r'<[^>]*>', '', str(text))).strip()
    # Long help/status reports are not error notices simply because they describe moderation.
    if default <= 0:
        return default
    return NOTICE_SECONDS if len(plain) < 700 and FAILURE.search(plain) else default
