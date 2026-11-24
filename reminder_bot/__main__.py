import logging
import os
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from telegram import LinkPreviewOptions, Update
from telegram.error import RetryAfter, TimedOut
from telegram.ext import ApplicationBuilder, Defaults

from .bot import ReminderBot
from .storage import Store


def main() -> None:
    load_dotenv(Path(__file__).resolve().parent.parent / '.env')
    token = os.getenv('TELEGRAM_BOT_TOKEN', '').strip()
    if not token:
        raise SystemExit('Укажите TELEGRAM_BOT_TOKEN в файле .env или в переменной окружения.')
    if not token.partition(':')[0].isdecimal():
        raise SystemExit('Проверьте формат TELEGRAM_BOT_TOKEN.')
    zone_name = os.getenv('DEFAULT_TIMEZONE', 'Europe/Moscow')
    try:
        zone = ZoneInfo(zone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise SystemExit(f'Неизвестный часовой пояс DEFAULT_TIMEZONE: {zone_name}') from None

    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(name)s %(message)s')
    logging.getLogger('httpx').setLevel(logging.WARNING)

    store = Store(Path(os.getenv('REMINDER_DB_PATH', 'data/reminder.sqlite3')).expanduser(), zone)
    bot = ReminderBot(store)
    app = (ApplicationBuilder()
           .token(token)
           .defaults(Defaults(link_preview_options=LinkPreviewOptions(is_disabled=True)))
           .post_init(bot.start)
           .post_stop(bot.stop)
           .post_shutdown(bot.close)
           .build())
    bot.register(app)
    logging.getLogger(__name__).info('Подключение к Telegram Bot API')
    try:
        app.run_polling(allowed_updates=[Update.MESSAGE, Update.CALLBACK_QUERY])
    except RetryAfter as error:
        delay = error.retry_after
        seconds = int(delay.total_seconds()) if isinstance(delay, timedelta) else delay
        raise SystemExit(f'Telegram ограничил запросы при запуске. Повторите через {seconds} сек.') from None
    except TimedOut:
        raise SystemExit(
            'Telegram Bot API не ответил после нескольких попыток. Проверьте доступ к api.telegram.org.'
        ) from None


if __name__ == '__main__':
    main()
