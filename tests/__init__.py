"""回归测试（只用标准库 unittest，不依赖网络与真实 Telegram）。

在仓库根目录运行全部测试：

    python3 -m unittest discover -s tests -t . -v

单个文件：

    python3 -m unittest tests.test_verification -v

覆盖范围：更新分发与消息生命周期（test_bot）、入群验证状态机与卡片（test_verification）、
AI 问答管道（test_ai）、汇率（test_exchange）、卡 BIN（test_binlookup）、
共用 HTTP 封装（test_httpjson）、命令菜单脚本（test_command_menus）。
"""
