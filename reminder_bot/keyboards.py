from calendar import Calendar
from datetime import date
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton as Button, InlineKeyboardMarkup as Markup

from .schedule import DAY, HOUR, local_datetime, shift_month
from .storage import Task

MONTHS = (
    "Январь",
    "Февраль",
    "Март",
    "Апрель",
    "Май",
    "Июнь",
    "Июль",
    "Август",
    "Сентябрь",
    "Октябрь",
    "Ноябрь",
    "Декабрь",
)
WEEKDAYS = ("Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс")
FULL_WEEKDAYS = (
    "понедельник",
    "вторник",
    "среда",
    "четверг",
    "пятница",
    "суббота",
    "воскресенье",
)
UNITS = {"m": ("мин", 60), "h": ("ч", HOUR), "d": ("дн", DAY), "w": ("нед", 7 * DAY)}
OFFSETS = {
    7 * DAY: "За 1 неделю",
    DAY: "За 1 день",
    HOUR: "За 1 час",
    300: "За 5 минут",
    0: "В момент дедлайна",
}
CUSTOM_OFFSETS_PAGE_SIZE = 8
Panel = tuple[str, Markup | None]


def format_duration(seconds: int) -> str:
    for label, unit in reversed(tuple(UNITS.values())):
        if seconds % unit == 0:
            return f"{seconds // unit} {label}"
    return f"{seconds} сек"


def format_time(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_date(timestamp: int, zone: ZoneInfo) -> str:
    return f"{local_datetime(timestamp, zone):%d.%m.%Y в %H:%M}"


def short(text: str, size: int) -> str:
    return text[: size - 1].rstrip() + "…" if len(text) > size else text


def month_code(day: date) -> str:
    return f"{day.year:04d}{day.month:02d}"


def day_code(day: date) -> str:
    return f"{month_code(day)}{day.day:02d}"


class Card:
    def __init__(self, task: Task, zone: ZoneInfo):
        self.task = task
        self.zone = zone

    def button(
        self, label: str, action: str = "noop", *, style: str | None = None
    ) -> Button:
        return Button(label, callback_data=f"r:{self.task.id}:{action}", style=style)

    def plan_label(self) -> str:
        plan = self.task.plan
        match plan.kind:
            case "default":
                return "ежедневно в 00:00"
            case "once":
                return f"один раз {format_date(plan.starts_at, self.zone)}"
            case "daily":
                return f"ежедневно в {format_time(plan.minutes)}"
            case "weekly":
                return f"{FULL_WEEKDAYS[plan.weekday]}, в {format_time(plan.minutes)} каждую неделю"
            case "monthly":
                return f"{plan.month_day}-го числа каждого месяца в {format_time(plan.minutes)}"
            case "interval":
                return f"с интервалом {format_duration(plan.interval)}"
        return "выключены"

    def main(self, added: bool = False) -> Panel:
        task = self.task
        title = (
            "✅ Дело выполнено"
            if task.completed
            else "Дело добавлено" if added else "Дело"
        )
        text = f"{title}\n\n{short(task.body, 700)}"
        if task.completed:
            return text, Markup([[Button("Мои дела", callback_data="l:0")]])
        text += f"\n\nРасписание: {self.plan_label()}"
        if task.deadline is not None:
            text += f"\nДедлайн: {format_date(task.deadline, self.zone)}"
        first = (
            "Изменить дедлайн" if task.deadline is not None else "Установить дедлайн"
        )
        rows = [
            [self.button(first, "deadline")],
            [self.button("Частота и время", "plan")],
        ]
        if task.deadline is not None:
            rows.append([self.button("До дедлайна", "offsets")])
        rows.append(
            [
                self.button("✓ Выполнено", "done", style="success"),
                Button("Мои дела", callback_data="l:0"),
            ]
        )
        return text, Markup(rows)

    def calendar(self, purpose: str, month: date, today: date) -> Panel:
        rows = [[self.button(label) for label in WEEKDAYS]]
        for week in Calendar().monthdatescalendar(month.year, month.month):
            row = []
            for day in week:
                in_month = day.month == month.month
                action = (
                    f"date:{purpose}:{day_code(day)}"
                    if today <= day and day.year <= 9998
                    else "past"
                )
                style = "primary" if in_month else None
                if day == today:
                    style = "success"
                row.append(self.button(str(day.day), action, style=style))
            rows.append(row)
        previous, following = shift_month(month, -1), shift_month(month, 1)
        if purpose == "d" and self.task.deadline is not None:
            deadline_day = local_datetime(self.task.deadline, self.zone).date()
            if deadline_day >= today:
                rows.append(
                    [
                        self.button(
                            "Изменить только время", f"date:d:{day_code(deadline_day)}"
                        )
                    ]
                )
            rows.append([self.button("Убрать дедлайн", "clear_deadline")])
        rows.append([self.button("‹ Назад", "main" if purpose == "d" else "plan")])
        rows.append(
            [
                self.button(
                    "‹",
                    (
                        f"cal:{purpose}:{month_code(previous)}"
                        if previous.year >= 2
                        else "noop"
                    ),
                ),
                self.button(
                    f"{MONTHS[month.month - 1]} {month.year}",
                    f"months:{purpose}:{month_code(month)}",
                ),
                self.button(
                    "›",
                    (
                        f"cal:{purpose}:{month_code(following)}"
                        if following.year <= 9998
                        else "noop"
                    ),
                ),
            ]
        )
        label = (
            "Выберите дату дедлайна" if purpose == "d" else "Выберите дату напоминания"
        )
        return f"{label}\n{MONTHS[month.month - 1]} {month.year}", Markup(rows)

    def months(self, purpose: str, month: date) -> Panel:
        year = month.year
        buttons = [
            self.button(
                name,
                f"cal:{purpose}:{year:04d}{number:02d}",
                style="primary" if number == month.month else None,
            )
            for number, name in enumerate(MONTHS, 1)
        ]
        rows = [buttons[index : index + 3] for index in range(0, 12, 3)]
        rows.append(
            [
                self.button(
                    "‹ Год",
                    f"months:{purpose}:{year - 1:04d}01" if year > 2 else "noop",
                ),
                self.button(str(year)),
                self.button(
                    "Год ›",
                    f"months:{purpose}:{year + 1:04d}01" if year < 9998 else "noop",
                ),
            ]
        )
        rows.append(
            [self.button("‹ К календарю", f"cal:{purpose}:{month_code(month)}")]
        )
        return f"Выберите месяц\n{year}", Markup(rows)

    def time_picker(
        self, purpose: str, target: str, hour: int | None = None, exact: bool = False
    ) -> Panel:
        back = "plan"
        description = ""
        if purpose in ("d", "o"):
            back = f"cal:{purpose}:{target[:6]}"
            description = f"{target[-2:]}.{target[4:6]}.{target[:4]} · "
        elif purpose == "w":
            back = "week"
            description = f"{FULL_WEEKDAYS[int(target)].capitalize()} · "
        elif purpose == "m":
            back = "month"
            description = f"{target}-е число · "
        if hour is None:
            buttons = [
                self.button(f"{value:02d}", f"hour:{purpose}:{target}:{value}")
                for value in range(24)
            ]
            rows = [buttons[index : index + 6] for index in range(0, 24, 6)]
            label = "Выберите час"
        else:
            prefix = f"{purpose}:{target}:{hour}"
            buttons = [
                self.button(
                    f"{minute:02d}", f"save:{purpose}:{target}:{hour * 60 + minute}"
                )
                for minute in range(0, 60, 1 if exact else 5)
            ]
            rows = [buttons[index : index + 6] for index in range(0, len(buttons), 6)]
            if not exact:
                rows.append([self.button("Точная минута", f"exact:{prefix}")])
            back = f"hour:{prefix}" if exact else f"hours:{purpose}:{target}"
            label = f"Выберите минуту для {hour:02d}:__"
        rows.append([self.button("‹ Назад", back)])
        return f"{label}\n{description}{self.zone.key}", Markup(rows)

    def plan(self) -> Panel:
        options = [
            ("default", "По умолчанию", "default"),
            ("once", "Один раз", "once"),
            ("daily", "Каждый день", "daily"),
            ("weekly", "Каждую неделю", "week"),
            ("monthly", "Каждый месяц", "month"),
            ("interval", "Свой интервал", "duration:i:h:0"),
            ("off", "Выключить", "off"),
        ]
        buttons = [
            self.button(f"✓ {label}" if kind == self.task.plan.kind else label, action)
            for kind, label, action in options
        ]
        rows = [buttons[index : index + 2] for index in range(0, len(buttons), 2)]
        rows.append([self.button("‹ К делу", "main")])
        return f"Частота и время напоминаний\n\nСейчас: {self.plan_label()}", Markup(
            rows
        )

    def weekdays(self) -> Panel:
        buttons = [
            self.button(label.capitalize(), f"hours:w:{day}")
            for day, label in enumerate(FULL_WEEKDAYS)
        ]
        rows = [buttons[index : index + 2] for index in range(0, 7, 2)]
        rows.append([self.button("‹ Назад", "plan")])
        return "Выберите день недели", Markup(rows)

    def monthdays(self) -> Panel:
        buttons = [self.button(str(day), f"hours:m:{day}") for day in range(1, 32)]
        rows = [buttons[index : index + 7] for index in range(0, len(buttons), 7)]
        rows.append([self.button("‹ Назад", "plan")])
        return "Выберите число месяца. В коротком месяце напоминание придёт в последний день.", Markup(rows)

    def offsets(self, page: int = 0) -> Panel:
        buttons = [
            self.button(
                f"✓ {label}" if seconds in self.task.offsets else label,
                f"toggle:{seconds}",
            )
            for seconds, label in OFFSETS.items()
        ]
        rows = [buttons[:2], buttons[2:4], buttons[4:]]
        deadline = local_datetime(self.task.deadline, self.zone)
        daily_label = f"Каждый день в {deadline:%H:%M}"
        rows.append(
            [
                self.button(
                    f"✓ {daily_label}" if self.task.deadline_daily else daily_label,
                    "toggle_deadline_daily",
                )
            ]
        )
        all_selected = self.task.deadline_daily and set(OFFSETS).issubset(
            self.task.offsets
        )
        rows.append(
            [
                self.button(
                    "✓ Все варианты выбраны" if all_selected else "Выбрать всё",
                    "all_deadline_offsets",
                )
            ]
        )
        rows.append([self.button("+ Своё напоминание", "duration:r:h:0")])
        custom = self.task.custom_offsets
        page_size = CUSTOM_OFFSETS_PAGE_SIZE
        last_page = max(0, (len(custom) - 1) // page_size)
        page = min(max(page, 0), last_page)
        for seconds in custom[page * page_size : (page + 1) * page_size]:
            rows.append(
                [
                    self.button(
                        f"✕ Убрать за {format_duration(seconds)}",
                        f"remove_offset:{seconds}:{page}",
                    )
                ]
            )
        navigation = []
        if page:
            navigation.append(self.button("‹ Назад по списку", f"offsets:{page - 1}"))
        if page < last_page:
            navigation.append(self.button("Дальше ›", f"offsets:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([self.button("‹ К делу", "main")])
        text = f"Напоминания до дедлайна\nДедлайн: {format_date(self.task.deadline, self.zone)}"
        return (
            text + "\n\nМожно выбрать несколько. Прошедшие моменты пропускаются.",
            Markup(rows),
        )

    def duration(self, scope: str, unit: str, value: int) -> Panel:
        prefix = f"duration:{scope}:{unit}"
        rows = [
            [
                self.button(
                    f"✓ {label}" if name == unit else label,
                    f"duration:{scope}:{name}:{value}",
                )
                for name, (label, _) in UNITS.items()
            ]
        ]
        digits = [
            self.button(
                str(digit), f"{prefix}:{value * 10 + digit}" if value < 100 else "noop"
            )
            for digit in range(1, 10)
        ]
        rows.extend(digits[index : index + 3] for index in range(0, 9, 3))
        rows.append(
            [
                self.button("Сброс", f"{prefix}:0"),
                self.button("0", f"{prefix}:{value * 10}" if value < 100 else "noop"),
                self.button("⌫", f"{prefix}:{value // 10}"),
            ]
        )
        if value:
            rows.append(
                [
                    self.button(
                        "Сохранить",
                        f"set_duration:{scope}:{unit}:{value}",
                        style="success",
                    )
                ]
            )
        rows.append([self.button("‹ Назад", "plan" if scope == "i" else "offsets")])
        title = (
            "Задайте интервал повторения"
            if scope == "i"
            else "За сколько напомнить до дедлайна?"
        )
        return f"{title}\n\n{value} {UNITS[unit][0]}", Markup(rows)
