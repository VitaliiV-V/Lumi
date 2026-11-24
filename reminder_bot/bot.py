import asyncio
import logging
from dataclasses import replace
from datetime import date, timedelta
from html import escape
from weakref import WeakValueDictionary
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telegram import (
    InlineKeyboardButton, InlineKeyboardMarkup, Message, Update,
)
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, NetworkError, RetryAfter, TelegramError
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from .keyboards import CUSTOM_OFFSETS_PAGE_SIZE, OFFSETS, UNITS, Card, Panel, format_date, format_duration, short
from .schedule import DAY, Plan, local_datetime, utc_now, wall_timestamp
from .storage import Job, Store, Task

LOG = logging.getLogger(__name__)
PRIVATE = filters.ChatType.PRIVATE
PRIVATE_MESSAGE = filters.UpdateType.MESSAGE & PRIVATE & ~filters.StatusUpdate.ALL
PAGE_SIZE = 8
ALL_DEADLINE_OFFSETS = tuple(sorted(OFFSETS))
MEDIA_NAMES = {
    'photo': 'Фото', 'video': 'Видео', 'voice': 'Голосовое сообщение', 'video_note': 'Видеосообщение',
    'document': 'Документ', 'audio': 'Аудио', 'sticker': 'Стикер', 'animation': 'Анимация',
    'location': 'Местоположение', 'venue': 'Место', 'contact': 'Контакт', 'poll': 'Опрос', 'dice': 'Игра',
}


def message_body(message: Message) -> str:
    if message.text:
        return message.text
    if message.caption:
        return message.caption
    if message.document and message.document.file_name:
        return f'Документ · {message.document.file_name}'
    if message.poll:
        return f'Опрос · {message.poll.question}'
    if message.sticker and message.sticker.emoji:
        return f'Стикер · {message.sticker.emoji}'
    return next((label for attribute, label in MEDIA_NAMES.items() if getattr(message, attribute)), 'Сообщение Telegram')


class ButtonError(ValueError):
    pass


class ReminderBot:
    def __init__(self, store: Store):
        self.store = store
        self.locks: WeakValueDictionary[int, asyncio.Lock] = WeakValueDictionary()
        self.stop_event = asyncio.Event()
        self.worker: asyncio.Task | None = None
        self.store_open = False

    def lock_for(self, task_id: int) -> asyncio.Lock:
        return self.locks.setdefault(task_id, asyncio.Lock())

    def register(self, app: Application) -> None:
        app.add_handler(CommandHandler(('start', 'help'), self.help, filters=PRIVATE))
        app.add_handler(CommandHandler('tasks', self.tasks, filters=PRIVATE))
        app.add_handler(CommandHandler('timezone', self.timezone, filters=PRIVATE))
        app.add_handler(CallbackQueryHandler(self.callback, pattern=r'^(r|l):'))
        app.add_handler(MessageHandler(PRIVATE_MESSAGE, self.add))
        app.add_error_handler(self.log_error)

    async def start(self, app: Application) -> None:
        await self.store.open()
        self.store_open = True
        self.stop_event.clear()
        self.worker = asyncio.create_task(self.deliver(app))

    async def stop(self, app: Application) -> None:
        if self.worker is not None:
            self.stop_event.set()
            await self.worker
            self.worker = None

    async def close(self, app: Application) -> None:
        await self.stop(app)
        if self.store_open:
            await self.store.close()
            self.store_open = False

    @staticmethod
    async def log_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        LOG.error('Ошибка при обработке обновления Telegram', exc_info=context.error)

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        zone = await self.store.zone(update.effective_user.id)
        await self.reply(
            update.effective_message,
            '<b>Привет! Я Луми — твоя помощница по напоминаниям!</b>\n\n'
            'Отправь мне текст, фото, файл или голосовое — я сохраню дело и буду напоминать о нём, '
            'пока ты его не выполнишь.\n\n'
            '📅 В карточке дела можно настроить дедлайн, время и частоту напоминаний.\n\n'
            f'📋 <b>Мои дела:</b> /tasks\n🌍 <b>Часовой пояс:</b> {escape(zone.key)} — /timezone',
            parse_mode=ParseMode.HTML,
        )

    async def tasks(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        text, markup = await self.task_list(update.effective_user.id, update.effective_chat.id, 0)
        await self.reply(update.effective_message, text, reply_markup=markup)

    async def task_list(self, user_id: int, chat_id: int, before: int) -> Panel:
        count, tasks = await self.store.active_page(user_id, chat_id, before, PAGE_SIZE + 1)
        zone = await self.store.zone(user_id)
        rows = []
        for task in tasks[:PAGE_SIZE]:
            label = ' '.join(task.body.split())
            if task.deadline is not None:
                label = f'{short(label, 22)} · {local_datetime(task.deadline, zone):%d.%m.%Y %H:%M}'
            rows.append([InlineKeyboardButton(short(label, 48), callback_data=f'r:{task.id}:main')])
        navigation = []
        if before:
            navigation.append(InlineKeyboardButton('К началу', callback_data='l:0'))
        if len(tasks) > PAGE_SIZE:
            navigation.append(InlineKeyboardButton('Дальше ›', callback_data=f'l:{tasks[PAGE_SIZE - 1].id}'))
        if navigation:
            rows.append(navigation)
        text = f'Активных дел — {count}'
        return text, InlineKeyboardMarkup(rows) if rows else None

    async def timezone(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        user_id = update.effective_user.id
        message = update.effective_message
        if not context.args:
            zone = await self.store.zone(user_id)
            await self.reply(message, f'Текущий часовой пояс: {zone.key}\nПример смены: /timezone Europe/Moscow')
            return
        key = ' '.join(context.args)
        try:
            zone = ZoneInfo(key)
        except (ZoneInfoNotFoundError, ValueError):
            await self.reply(message, 'Неизвестный часовой пояс. Укажите название IANA, например Asia/Yekaterinburg.')
            return
        await self.store.set_zone(user_id, zone, utc_now())
        await self.reply(message, f'Часовой пояс установлен: {zone.key}')

    async def reply(self, message: Message, text: str, **kwargs) -> None:
        bot = message.get_bot()
        await bot.send_message(message.chat_id, text, **kwargs)
        if message.from_user is None or message.from_user.is_bot:
            return
        # Telegram permits deleting private dice messages only after 24 hours.
        await self.store.remember_processed_message(
            bot.id, message.chat_id, message.message_id, int(message.date.timestamp()),
            delete_delay=DAY + 1 if message.dice else 0,
        )
        await self.clear_processed_messages(message)

    async def clear_processed_messages(self, latest: Message) -> None:
        bot = latest.get_bot()
        while True:
            ids = await self.store.messages_to_delete(bot.id, latest.chat_id, latest.message_id, utc_now())
            if not ids:
                return
            try:
                await bot.delete_messages(latest.chat_id, ids)
            except TelegramError as error:
                LOG.warning('Не удалось очистить старые сообщения (%s)', type(error).__name__)
                return
            await self.store.forget_messages(bot.id, latest.chat_id, ids)
            if len(ids) < 100:
                return

    async def add(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        message = update.effective_message
        user_id = update.effective_user.id
        task, created = await self.store.create(user_id, message.chat_id, message.message_id, message_body(message), utc_now())
        if created:
            text, markup = Card(task, await self.store.zone(user_id)).main(added=True)
            await self.reply(message, text, reply_markup=markup)

    async def callback(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        chat = update.effective_chat
        if chat is None or chat.type != 'private':
            await query.answer('Откройте личный чат с ботом.', show_alert=True)
            return
        user_id = query.from_user.id
        try:
            prefix, raw_id, *parts = query.data.split(':')
            key = int(raw_id)
            if not 0 <= key < 2**63:
                raise ValueError
            if prefix == 'l' and not parts:
                panel = await self.task_list(user_id, chat.id, key)
            elif prefix == 'r' and parts:
                async with self.lock_for(key):
                    task = await self.store.task(key, user_id, chat.id)
                    if task is None:
                        raise ButtonError('Дело недоступно.')
                    card = Card(task, await self.store.zone(user_id))
                    panel = await self.action(card, parts)
            else:
                raise ValueError
        except ButtonError as error:
            await query.answer(str(error), show_alert=True)
            return
        except (ValueError, OverflowError):
            await query.answer('Кнопка устарела. Откройте дело через /tasks.', show_alert=True)
            return
        await query.answer()
        if panel is not None:
            text, markup = panel
            try:
                await query.edit_message_text(text, reply_markup=markup)
            except BadRequest as error:
                if 'message is not modified' not in str(error).lower():
                    raise

    async def action(self, card: Card, parts: list[str]) -> Panel | None:
        task, zone = card.task, card.zone
        now = utc_now()
        today = local_datetime(now, zone).date()
        if task.completed or parts == ['main']:
            return card.main()
        match parts:
            case ['noop']:
                return None
            case ['past']:
                raise ButtonError('Эта дата уже прошла.')
            case ['done']:
                await self.store.complete(task)
                return Card(replace(task, completed=True), zone).main()
            case ['deadline' | 'once' as action]:
                return card.calendar('d' if action == 'deadline' else 'o', today.replace(day=1), today)
            case ['cal' | 'months' as action, 'd' | 'o' as purpose, code]:
                month = self.parse_day(code + '01')
                if action == 'cal':
                    return card.calendar(purpose, month, today)
                return card.months(purpose, month)
            case ['plan']:
                return card.plan()
            case ['default' | 'off' as kind]:
                return await self.save_plan(card, Plan(kind=kind), now)
            case ['week']:
                return card.weekdays()
            case ['month']:
                return card.monthdays()
            case ['daily']:
                return card.time_picker('a', '0')
            case ['date' | 'hours' as action, purpose, target]:
                self.validate_target(purpose, target, today)
                if action == 'date' and purpose not in ('d', 'o'):
                    raise ValueError
                return card.time_picker(purpose, target)
            case ['hour' | 'exact' as action, purpose, target, hour]:
                self.validate_target(purpose, target, today)
                if not 0 <= int(hour) < 24:
                    raise ValueError
                return card.time_picker(purpose, target, int(hour), exact=action == 'exact')
            case ['save', purpose, target, minutes]:
                self.validate_target(purpose, target, today)
                if not 0 <= int(minutes) < 1440:
                    raise ValueError
                return await self.save_time(card, purpose, target, int(minutes), now)
            case ['offsets'] if task.deadline is not None:
                return card.offsets()
            case ['offsets', page] if task.deadline is not None:
                return card.offsets(int(page))
            case ['clear_deadline']:
                await self.store.set_deadline(task, None, (), (), now)
                return Card(replace(task, deadline=None, offsets=(), custom_offsets=(), deadline_daily=False), zone).main()
            case ['toggle', value] if task.deadline is not None and int(value) in OFFSETS:
                offsets = tuple(sorted(set(task.offsets) ^ {int(value)}))
                return await self.save_offsets(card, offsets, task.custom_offsets, now)
            case ['toggle_deadline_daily'] if task.deadline is not None:
                return await self.save_offsets(card, task.offsets, task.custom_offsets, now, daily=not task.deadline_daily)
            case ['all_deadline_offsets'] if task.deadline is not None:
                return await self.save_offsets(card, ALL_DEADLINE_OFFSETS, task.custom_offsets, now, daily=True)
            case ['remove_offset', seconds, page] if task.deadline is not None:
                offset = int(seconds)
                custom_offsets = tuple(value for value in task.custom_offsets if value != offset)
                return await self.save_offsets(card, task.offsets, custom_offsets, now, page=int(page))
            case ['clear_offset'] if task.deadline is not None:
                if len(task.custom_offsets) == 1:
                    return await self.save_offsets(card, task.offsets, (), now)
                return card.offsets()
            case ['duration' | 'set_duration' as action, 'i' | 'r' as scope, unit, value] if unit in UNITS:
                return await self.duration_action(card, action, scope, unit, int(value), now)
        raise ValueError

    @staticmethod
    def parse_day(code: str) -> date:
        if len(code) != 8 or not code.isascii() or not code.isdecimal():
            raise ValueError
        day = date(int(code[:4]), int(code[4:6]), int(code[6:]))
        if not 2 <= day.year <= 9998:
            raise ValueError
        return day

    def validate_target(self, purpose: str, target: str, today: date) -> None:
        if purpose in ('d', 'o'):
            if self.parse_day(target) < today:
                raise ButtonError('Эта дата уже прошла.')
        elif purpose == 'm':
            if not 1 <= int(target) <= 31:
                raise ValueError
        elif not (purpose == 'a' and target == '0' or purpose == 'w' and target in '0123456' and len(target) == 1):
            raise ValueError

    async def save_time(self, card: Card, purpose: str, target: str, minutes: int, now: int) -> Panel:
        task, zone = card.task, card.zone
        if purpose == 'a':
            return await self.save_plan(card, Plan(kind='daily', minutes=minutes), now)
        if purpose == 'w':
            return await self.save_plan(card, Plan(kind='weekly', minutes=minutes, weekday=int(target)), now)
        if purpose == 'm':
            return await self.save_plan(card, Plan(kind='monthly', minutes=minutes, month_day=int(target)), now)
        timestamp = wall_timestamp(self.parse_day(target), minutes, zone)
        if timestamp is None:
            raise ButtonError('Из-за перевода часов этого времени нет. Выберите другое.')
        if timestamp <= now:
            raise ButtonError('Это время уже прошло. Выберите время в будущем.')
        if purpose == 'o':
            return await self.save_plan(card, Plan(kind='once', starts_at=timestamp), now)
        offsets = ALL_DEADLINE_OFFSETS if task.deadline is None else task.offsets
        daily = task.deadline is None or task.deadline_daily
        await self.store.set_deadline(task, timestamp, offsets, task.custom_offsets, now, daily=daily)
        return Card(replace(task, deadline=timestamp, offsets=offsets, deadline_daily=daily), zone).main()

    async def save_plan(self, card: Card, plan: Plan, now: int) -> Panel:
        await self.store.set_plan(card.task, plan, card.zone, now)
        return Card(replace(card.task, plan=plan), card.zone).main()

    async def save_offsets(
        self, card: Card, offsets: tuple[int, ...], custom_offsets: tuple[int, ...], now: int,
        *, daily: bool | None = None, page: int = 0,
    ) -> Panel:
        task = card.task
        daily = task.deadline_daily if daily is None else daily
        await self.store.set_deadline(task, task.deadline, offsets, custom_offsets, now, daily=daily)
        updated = replace(task, offsets=offsets, custom_offsets=custom_offsets, deadline_daily=daily)
        return Card(updated, card.zone).offsets(page)

    async def duration_action(self, card: Card, action: str, scope: str, unit: str, value: int, now: int) -> Panel:
        if not 0 <= value <= 999 or (scope == 'r' and card.task.deadline is None):
            raise ValueError
        if action == 'duration':
            return card.duration(scope, unit, value)
        if not value:
            raise ValueError
        seconds = value * UNITS[unit][1]
        if scope == 'r':
            custom_offsets = tuple(sorted(set(card.task.custom_offsets) | {seconds}))
            page = custom_offsets.index(seconds) // CUSTOM_OFFSETS_PAGE_SIZE
            return await self.save_offsets(card, card.task.offsets, custom_offsets, now, page=page)
        return await self.save_plan(card, Plan(kind='interval', starts_at=now, interval=seconds), now)

    async def wait(self, seconds: float) -> None:
        try:
            await asyncio.wait_for(self.stop_event.wait(), seconds)
        except TimeoutError:
            pass

    async def deliver(self, app: Application) -> None:
        while not self.stop_event.is_set():
            try:
                jobs = await self.store.due(utc_now())
                if not jobs:
                    await self.wait(2)
                for job in jobs:
                    if self.stop_event.is_set():
                        break
                    async with self.lock_for(job.task_id):
                        pause = await self.send(app, job)
                    if pause:
                        await self.wait(pause)
            except Exception:
                LOG.exception('Ошибка очереди напоминаний')
                await self.wait(5)

    async def send(self, app: Application, job: Job) -> int:
        delivery = await self.store.delivery(job)
        if delivery is None:
            return 0
        task, zone = delivery
        heading = '🔔 Напоминание'
        if job.kind == 'deadline_daily':
            now = utc_now()
            if task.deadline is None or now >= task.deadline:
                await self.store.finish_job(job, task, zone, now)
                return 0
            heading = '🔔 Напоминание о дедлайне'
        if job.kind == 'deadline' and task.deadline is not None:
            offset = task.deadline - job.scheduled_at
            label = OFFSETS.get(offset) or f'За {format_duration(offset)}'
            if offset and utc_now() >= task.deadline:
                label = 'Дедлайн прошёл'
            heading = f'🔔 {label}'
        text = f'{heading}\n\n{short(task.body, 700)}'
        if task.deadline is not None:
            text += f'\n\nДедлайн: {format_date(task.deadline, zone)}'
        card = Card(task, zone)
        markup = InlineKeyboardMarkup([[card.button('Открыть дело', 'main'), card.button('✓ Выполнено', 'done', style='success')]])
        try:
            await app.bot.send_message(task.chat_id, text, reply_markup=markup)
        except RetryAfter as error:
            delay = error.retry_after
            seconds = int(delay.total_seconds() if isinstance(delay, timedelta) else delay) + 1
            await self.store.postpone(job, utc_now() + seconds)
            return seconds
        except Forbidden:
            await self.store.postpone(job, utc_now() + DAY)
        except BadRequest:
            LOG.warning('Telegram отклонил напоминание %s для дела %s', job.id, task.id, exc_info=True)
            await self.store.finish_job(job, task, zone, utc_now())
        except NetworkError:
            await self.store.postpone(job, utc_now() + min(300, 5 * 2 ** min(job.attempts, 6)))
        else:
            await self.store.finish_job(job, task, zone, utc_now())
        return 0
