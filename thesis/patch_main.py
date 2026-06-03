#!/usr/bin/env python3
import sys

# Read main.py
with open('/Users/ahs/thesis/main.py', 'r') as f:
    content = f.read()

# PATCH 1: Add import for TelegramBotHandler
old_import = "from clients.telegram_client import TelegramClient"
new_import = "from clients.telegram_client import TelegramClient\nfrom clients.telegram_bot_handler import TelegramBotHandler"
if old_import in content:
    content = content.replace(old_import, new_import)
    print("✅ PATCH 1: Import added")

# PATCH 2: Add globals for telegram_bot and polling_task
old_globals = "global _chronicle, _oracle, _perplexity, _sovereign, _telegram\n    global _engine, _weekly_gen, _daily_gen, _monitor, _scheduler"
new_globals = "global _chronicle, _oracle, _perplexity, _sovereign, _telegram\n    global _engine, _weekly_gen, _daily_gen, _monitor, _scheduler\n    global _telegram_bot, _polling_task"
if old_globals in content:
    content = content.replace(old_globals, new_globals)
    print("✅ PATCH 2: Globals added")

# PATCH 3: Instantiate telegram_bot after telegram
old_telegram = "_telegram = TelegramClient(\n        bot_token=os.getenv(\"TELEGRAM_BOT_TOKEN\", \"\"),\n        chat_id=os.getenv(\"AHMED_CHAT_ID\", \"\"),\n    )"
new_telegram = "_telegram = TelegramClient(\n        bot_token=os.getenv(\"TELEGRAM_BOT_TOKEN\", \"\"),\n        chat_id=os.getenv(\"AHMED_CHAT_ID\", \"\"),\n    )\n    _telegram_bot = TelegramBotHandler(\n        bot_token=os.getenv(\"TELEGRAM_BOT_TOKEN\", \"\"),\n        chat_id=os.getenv(\"AHMED_CHAT_ID\", \"\"),\n        http_base_url=f\"http://localhost:{os.getenv('THESIS_PORT', '8060')}\",\n    )"
if old_telegram in content:
    content = content.replace(old_telegram, new_telegram)
    print("✅ PATCH 3: TelegramBotHandler instantiation added")

# PATCH 4: Start polling task after scheduler.start()
old_scheduler_log = "logger.info(\"THESIS service started on port %s\", os.getenv(\"THESIS_PORT\", \"8060\"))\n\n    yield"
new_scheduler_log = "logger.info(\"THESIS service started on port %s\", os.getenv(\"THESIS_PORT\", \"8060\"))\n    \n    # Start Telegram bot polling in background\n    _polling_task = asyncio.create_task(_telegram_bot.start_polling())\n    logger.info(\"Telegram bot polling started\")\n\n    yield"
if old_scheduler_log in content:
    content = content.replace(old_scheduler_log, new_scheduler_log)
    print("✅ PATCH 4: Polling task startup added")

# PATCH 5: Stop polling task on shutdown
old_shutdown = "_scheduler.shutdown(wait=False)\n    logger.info(\"THESIS service shutdown complete\")"
new_shutdown = "await _telegram_bot.stop_polling()\n    logger.info(\"Telegram bot polling stopped\")\n    \n    _scheduler.shutdown(wait=False)\n    logger.info(\"THESIS service shutdown complete\")"
if old_shutdown in content:
    content = content.replace(old_shutdown, new_shutdown)
    print("✅ PATCH 5: Polling task shutdown added")

# Write back
with open('/Users/ahs/thesis/main.py', 'w') as f:
    f.write(content)

print("\n✅ main.py patched successfully")
