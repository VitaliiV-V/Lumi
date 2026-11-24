import asyncio
import json
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from zoneinfo import ZoneInfo

import aiosqlite

from .schedule import DAY, HOUR, Plan, local_datetime, next_recurring, next_wall_time, regular_occurrences, utc_now

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY,
    timezone TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY,
    user_id INTEGER NOT NULL REFERENCES users(id),
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    body TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    completed INTEGER NOT NULL DEFAULT 0,
    plan TEXT NOT NULL,
    deadline INTEGER,
    offsets TEXT NOT NULL DEFAULT '[]',
    custom_offsets TEXT NOT NULL DEFAULT '[]',
    deadline_daily INTEGER NOT NULL DEFAULT 0,
    UNIQUE (chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS tasks_by_owner ON tasks (user_id, completed, id);
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    kind TEXT NOT NULL,
    scheduled_at INTEGER NOT NULL,
    available_at INTEGER NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    UNIQUE (task_id, kind, scheduled_at)
);
CREATE INDEX IF NOT EXISTS jobs_by_availability ON jobs (available_at, id);
CREATE TABLE IF NOT EXISTS processed_user_messages (
    bot_id INTEGER NOT NULL,
    chat_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    deletable_at INTEGER NOT NULL,
    PRIMARY KEY (bot_id, chat_id, message_id)
);
CREATE INDEX IF NOT EXISTS processed_user_messages_by_date ON processed_user_messages (sent_at);
'''


@dataclass(frozen=True)
class Task:
    id: int
    user_id: int
    chat_id: int
    message_id: int
    body: str
    created_at: int
    completed: bool
    plan: Plan
    deadline: int | None
    offsets: tuple[int, ...]
    custom_offsets: tuple[int, ...]
    deadline_daily: bool = False

    @classmethod
    def from_row(cls, row: aiosqlite.Row) -> 'Task':
        fields = dict(row)
        fields['plan'] = Plan(**json.loads(row['plan']))
        fields['offsets'] = tuple(json.loads(row['offsets']))
        fields['custom_offsets'] = tuple(json.loads(row['custom_offsets']))
        fields.pop('custom_offset', None)
        fields['completed'] = bool(row['completed'])
        fields['deadline_daily'] = bool(row['deadline_daily'])
        return cls(**fields)

    def next_deadline_daily(self, zone: ZoneInfo, after: int) -> tuple[str, int] | None:
        if not self.deadline_daily or self.deadline is None or self.deadline <= after:
            return None
        local = local_datetime(self.deadline, zone)
        minutes = local.hour * 60 + local.minute
        while True:
            due = next_wall_time(after, minutes, zone)
            if due >= self.deadline:
                return None
            offset = self.deadline - due
            if offset not in self.offsets and offset not in self.custom_offsets:
                return 'deadline_daily', due
            after = due


@dataclass(frozen=True)
class Job:
    id: int
    task_id: int
    kind: str
    scheduled_at: int
    available_at: int
    attempts: int


class Store:
    def __init__(self, path: Path, default_zone: ZoneInfo):
        self.path = path
        self.default_zone = default_zone
        self.lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = await aiosqlite.connect(self.path, isolation_level=None)
        self.db.row_factory = aiosqlite.Row
        try:
            await self.db.execute('PRAGMA foreign_keys = ON')
            await self.db.execute('PRAGMA journal_mode = WAL')
            await self.db.executescript(SCHEMA)
            async with self.write():
                columns = {column['name'] for column in await self.db.execute_fetchall('PRAGMA table_info(tasks)')}
                if 'deadline_daily' not in columns:
                    await self.db.execute('ALTER TABLE tasks ADD COLUMN deadline_daily INTEGER NOT NULL DEFAULT 0')
                if 'custom_offsets' not in columns:
                    await self.db.execute("ALTER TABLE tasks ADD COLUMN custom_offsets TEXT NOT NULL DEFAULT '[]'")
                    if 'custom_offset' in columns:
                        await self.db.execute(
                            "UPDATE tasks SET custom_offsets = '[' || custom_offset || ']' WHERE custom_offset IS NOT NULL",
                        )
                await self.db.execute("DELETE FROM jobs WHERE kind = 'initial'")
                now = utc_now()
                rows = await self.db.execute_fetchall(
                    '''SELECT jobs.id, jobs.scheduled_at, users.timezone FROM jobs
                       JOIN tasks ON tasks.id = jobs.task_id JOIN users ON users.id = tasks.user_id
                       WHERE jobs.kind = 'midnight' AND jobs.scheduled_at > ? AND tasks.created_at > ?''',
                    (now, now - 5 * HOUR),
                )
                for row in rows:
                    midnight = next_wall_time(now, 0, ZoneInfo(row['timezone']))
                    if midnight < row['scheduled_at']:
                        await self.db.execute('UPDATE jobs SET scheduled_at = ?, available_at = ? WHERE id = ?',
                                              (midnight, midnight, row['id']))
        except BaseException:
            await self.db.close()
            raise

    async def close(self) -> None:
        await self.db.close()

    @asynccontextmanager
    async def write(self):
        async with self.lock:
            try:
                await self.db.execute('BEGIN IMMEDIATE')
                yield
                await self.db.commit()
            except BaseException:
                await self.db.rollback()
                raise

    async def one(self, sql: str, values: tuple) -> aiosqlite.Row | None:
        async with self.db.execute(sql, values) as cursor:
            return await cursor.fetchone()

    async def _zone(self, user_id: int) -> ZoneInfo:
        row = await self.one('SELECT timezone FROM users WHERE id = ?', (user_id,))
        return ZoneInfo(row['timezone']) if row else self.default_zone

    async def zone(self, user_id: int) -> ZoneInfo:
        async with self.lock:
            return await self._zone(user_id)

    async def remember_processed_message(
        self, bot_id: int, chat_id: int, message_id: int, sent_at: int, *, delete_delay: int = 0,
    ) -> None:
        async with self.write():
            await self.db.execute('DELETE FROM processed_user_messages WHERE sent_at <= ?', (sent_at - 2 * DAY,))
            await self.db.execute(
                '''INSERT OR IGNORE INTO processed_user_messages (bot_id, chat_id, message_id, sent_at, deletable_at)
                   VALUES (?, ?, ?, ?, ?)''', (bot_id, chat_id, message_id, sent_at, sent_at + delete_delay),
            )

    async def messages_to_delete(self, bot_id: int, chat_id: int, through: int, now: int) -> list[int]:
        async with self.lock:
            rows = await self.db.execute_fetchall(
                '''SELECT message_id FROM processed_user_messages
                   WHERE bot_id = ? AND chat_id = ? AND message_id <= ? AND sent_at > ? AND deletable_at <= ?
                   ORDER BY message_id LIMIT 100''', (bot_id, chat_id, through, now - 2 * DAY, now),
            )
        return [row['message_id'] for row in rows]

    async def forget_messages(self, bot_id: int, chat_id: int, message_ids: list[int]) -> None:
        async with self.write():
            await self.db.executemany(
                'DELETE FROM processed_user_messages WHERE bot_id = ? AND chat_id = ? AND message_id = ?',
                [(bot_id, chat_id, message_id) for message_id in message_ids],
            )

    async def _insert_jobs(self, task_id: int, occurrences: list[tuple[str, int]]) -> None:
        await self.db.executemany(
            'INSERT INTO jobs (task_id, kind, scheduled_at, available_at) VALUES (?, ?, ?, ?)',
            [(task_id, kind, due, due) for kind, due in occurrences],
        )

    async def create(self, user_id: int, chat_id: int, message_id: int, body: str, now: int) -> tuple[Task, bool]:
        async with self.write():
            row = await self.one('SELECT * FROM tasks WHERE chat_id = ? AND message_id = ?', (chat_id, message_id))
            if row:
                return Task.from_row(row), False
            await self.db.execute(
                'INSERT OR IGNORE INTO users (id, timezone) VALUES (?, ?)', (user_id, self.default_zone.key),
            )
            plan = Plan()
            async with self.db.execute(
                '''INSERT INTO tasks (user_id, chat_id, message_id, body, created_at, plan)
                   VALUES (?, ?, ?, ?, ?, ?)''',
                (user_id, chat_id, message_id, body, now, json.dumps(asdict(plan))),
            ) as cursor:
                task = Task(
                    id=cursor.lastrowid, user_id=user_id, chat_id=chat_id, message_id=message_id, body=body,
                    created_at=now, completed=False, plan=plan, deadline=None, offsets=(), custom_offsets=(),
                )
            await self._insert_jobs(task.id, regular_occurrences(plan, await self._zone(user_id), now))
            return task, True

    async def task(self, task_id: int, user_id: int, chat_id: int) -> Task | None:
        async with self.lock:
            row = await self.one(
                'SELECT * FROM tasks WHERE id = ? AND user_id = ? AND chat_id = ?', (task_id, user_id, chat_id),
            )
        return Task.from_row(row) if row else None

    async def active_page(self, user_id: int, chat_id: int, before: int, limit: int) -> tuple[int, list[Task]]:
        async with self.lock:
            count = await self.one(
                'SELECT COUNT(*) FROM tasks WHERE user_id = ? AND chat_id = ? AND completed = 0',
                (user_id, chat_id),
            )
            cursor = await self.one(
                'SELECT COALESCE(deadline, 9223372036854775807) FROM tasks WHERE id = ? AND user_id = ? AND chat_id = ?',
                (before, user_id, chat_id),
            ) if before else None
            start = (cursor[0], -before) if cursor else (-2**63, 0)
            rows = await self.db.execute_fetchall(
                '''SELECT * FROM tasks WHERE user_id = ? AND chat_id = ? AND completed = 0
                   AND (COALESCE(deadline, 9223372036854775807), -id) > (?, ?)
                   ORDER BY COALESCE(deadline, 9223372036854775807), id DESC LIMIT ?''',
                (user_id, chat_id, *start, limit),
            )
        return count[0], [Task.from_row(row) for row in rows]

    async def set_plan(self, task: Task, plan: Plan, zone: ZoneInfo, now: int) -> None:
        if task.plan == plan:
            return
        async with self.write():
            await self.db.execute('UPDATE tasks SET plan = ? WHERE id = ?', (json.dumps(asdict(plan)), task.id))
            await self.db.execute(
                "DELETE FROM jobs WHERE task_id = ? AND kind NOT IN ('deadline', 'deadline_daily')", (task.id,),
            )
            await self._insert_jobs(task.id, regular_occurrences(plan, zone, now))

    async def set_deadline(
        self, task: Task, deadline: int | None, offsets: tuple[int, ...], custom_offsets: tuple[int, ...], now: int,
        *, daily: bool | None = None,
    ) -> None:
        daily = deadline is not None and (task.deadline_daily if daily is None else daily)
        updated = replace(task, deadline=deadline, offsets=offsets, custom_offsets=custom_offsets, deadline_daily=daily)
        if updated == task:
            return
        seconds = set(offsets).union(custom_offsets)
        times = {deadline - offset for offset in seconds} if deadline is not None else set()
        async with self.write():
            await self.db.execute(
                'UPDATE tasks SET deadline = ?, offsets = ?, custom_offsets = ?, deadline_daily = ? WHERE id = ?',
                (deadline, json.dumps(offsets), json.dumps(custom_offsets), daily, task.id),
            )
            rows = await self.db.execute_fetchall(
                "SELECT id, scheduled_at FROM jobs WHERE task_id = ? AND kind = 'deadline'", (task.id,),
            )
            await self.db.executemany(
                'DELETE FROM jobs WHERE id = ?', [(row['id'],) for row in rows if row['scheduled_at'] not in times],
            )
            pending = {row['scheduled_at'] for row in rows}
            await self._insert_jobs(task.id, [('deadline', due) for due in sorted(times - pending) if due > now])
            await self._refresh_deadline_daily(updated, await self._zone(task.user_id), now)

    async def _refresh_deadline_daily(self, task: Task, zone: ZoneInfo, now: int) -> None:
        occurrence = task.next_deadline_daily(zone, now)
        await self.db.execute(
            "DELETE FROM jobs WHERE task_id = ? AND kind = 'deadline_daily' AND scheduled_at != ?",
            (task.id, occurrence[1] if occurrence else -1),
        )
        if occurrence:
            kind, due = occurrence
            await self.db.execute(
                'INSERT OR IGNORE INTO jobs (task_id, kind, scheduled_at, available_at) VALUES (?, ?, ?, ?)',
                (task.id, kind, due, due),
            )

    async def complete(self, task: Task) -> None:
        async with self.write():
            await self.db.execute('UPDATE tasks SET completed = 1 WHERE id = ?', (task.id,))
            await self.db.execute('DELETE FROM jobs WHERE task_id = ?', (task.id,))

    async def set_zone(self, user_id: int, zone: ZoneInfo, now: int) -> None:
        async with self.write():
            previous = await self._zone(user_id)
            await self.db.execute(
                '''INSERT INTO users (id, timezone) VALUES (?, ?)
                   ON CONFLICT (id) DO UPDATE SET timezone = excluded.timezone''', (user_id, zone.key),
            )
            if previous.key == zone.key:
                return
            rows = await self.db.execute_fetchall('SELECT * FROM tasks WHERE user_id = ? AND completed = 0', (user_id,))
            for row in rows:
                task = Task.from_row(row)
                if task.plan.kind in ('default', 'daily', 'weekly', 'monthly'):
                    await self.db.execute(
                        "DELETE FROM jobs WHERE task_id = ? AND kind IN ('midnight', 'daily', 'weekly', 'monthly')", (task.id,),
                    )
                    await self._insert_jobs(task.id, [next_recurring(task.plan, zone, now)])
                if task.deadline_daily:
                    await self._refresh_deadline_daily(task, zone, now)

    async def due(self, now: int) -> list[Job]:
        async with self.lock:
            rows = await self.db.execute_fetchall(
                'SELECT * FROM jobs WHERE available_at <= ? ORDER BY available_at, id LIMIT 50', (now,),
            )
        return [Job(**row) for row in rows]

    async def delivery(self, job: Job) -> tuple[Task, ZoneInfo] | None:
        async with self.lock:
            row = await self.one(
                'SELECT tasks.* FROM tasks JOIN jobs ON jobs.task_id = tasks.id WHERE jobs.id = ? AND tasks.completed = 0',
                (job.id,),
            )
            if row:
                return Task.from_row(row), await self._zone(row['user_id'])
        return None

    async def postpone(self, job: Job, available_at: int) -> None:
        async with self.write():
            await self.db.execute(
                'UPDATE jobs SET available_at = ?, attempts = attempts + 1 WHERE id = ?', (available_at, job.id),
            )

    async def finish_job(self, job: Job, task: Task, zone: ZoneInfo, now: int) -> None:
        async with self.write():
            async with self.db.execute('DELETE FROM jobs WHERE id = ?', (job.id,)) as cursor:
                deleted = cursor.rowcount
            if not deleted or job.kind in ('initial', 'once', 'deadline'):
                return
            after = max(job.scheduled_at, now)
            if job.kind == 'deadline_daily':
                occurrence = task.next_deadline_daily(zone, after)
            else:
                occurrence = next_recurring(task.plan, zone, after)
            if occurrence:
                await self._insert_jobs(task.id, [occurrence])
