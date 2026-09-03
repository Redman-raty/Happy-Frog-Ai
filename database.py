import aiosqlite
from zoneinfo import ZoneInfo
from datetime import datetime, date, timezone
from config import DB_PATH, MAX_REQUESTS_PER_DAY
from typing import List, Dict
import logging
import os


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA journal_mode=WAL;")          # быстрее/надёжнее при конкурентном доступе
        await db.execute("PRAGMA synchronous=NORMAL;")
        await db.execute("PRAGMA busy_timeout=30000;")

        # users
        await db.execute("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER UNIQUE,
                username TEXT,
                first_name TEXT,
                registration_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_vip INTEGER DEFAULT 0,
                timezone TEXT DEFAULT 'Europe/Moscow'
            )
        """)

        # user_requests
        await db.execute("""
            CREATE TABLE IF NOT EXISTS user_requests (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                request_date DATE,
                request_count INTEGER DEFAULT 0,
                UNIQUE(user_id, request_date),
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)

        # conversations
        await db.execute("""
            CREATE TABLE IF NOT EXISTS conversations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                role TEXT,
                content TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                is_active INTEGER DEFAULT 1,
                FOREIGN KEY (user_id) REFERENCES users (user_id)
            )
        """)

        # todo_tasks
        await db.execute("""
            CREATE TABLE IF NOT EXISTS todo_tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                text TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed INTEGER DEFAULT 0,
                completed_at TIMESTAMP NULL,
                completed_at_ts INTEGER NULL,

                remind_at_utc TIMESTAMP NULL,
                remind_at_local TEXT NULL,
                remind_at_ts INTEGER NULL,

                notify_at_ts INTEGER NULL,
                notify_at_local TEXT NULL,
                notify_offset_min INTEGER DEFAULT 0,

                reminded INTEGER DEFAULT 0
            )
        """)

        # ---- миграции: добавляем недостающие колонки только если их реально нет ----
        async def _ensure_column(table: str, col: str, ddl: str):
            cur = await db.execute(f"PRAGMA table_info({table})")
            cols = {row[1] for row in await cur.fetchall()}  # row[1] = name
            await cur.close()
            if col not in cols:
                await db.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

        # users.timezone (если вдруг старая БД)
        await _ensure_column("users", "timezone", "timezone TEXT DEFAULT 'Europe/Moscow'")

        # todo_tasks columns (если старая БД)
        await _ensure_column("todo_tasks", "remind_at_utc", "remind_at_utc TIMESTAMP NULL")
        await _ensure_column("todo_tasks", "remind_at_local", "remind_at_local TEXT NULL")
        await _ensure_column("todo_tasks", "remind_at_ts", "remind_at_ts INTEGER NULL")
        await _ensure_column("todo_tasks", "completed_at_ts", "completed_at_ts INTEGER NULL")
        await _ensure_column("todo_tasks", "notify_at_ts", "notify_at_ts INTEGER NULL")
        await _ensure_column("todo_tasks", "notify_at_local", "notify_at_local TEXT NULL")
        await _ensure_column("todo_tasks", "notify_offset_min", "notify_offset_min INTEGER DEFAULT 0")
        await _ensure_column("todo_tasks", "reminded", "reminded INTEGER DEFAULT 0")

        # Нормализация timezone в users
        await db.execute("""
            UPDATE users
            SET timezone = 'Europe/Moscow'
            WHERE timezone IS NULL OR timezone = '' OR timezone = 'UTC'
        """)

        # ---- Индексы ----
        # Ускоряет воркер
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_todo_worker
            ON todo_tasks (completed, reminded, notify_at_ts)
        """)

        # Ускоряет выборки задач по пользователю
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_todo_user_created
            ON todo_tasks (user_id, created_at)
        """)

        # Ускоряет выборки "за сегодня" (completed + completed_at_ts)
        await db.execute("""
            CREATE INDEX IF NOT EXISTS idx_todo_user_completed_ts
            ON todo_tasks (user_id, completed, completed_at_ts)
        """)

        await db.commit()


async def set_user_timezone(user_id: int, timezone: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "UPDATE users SET timezone = ? WHERE user_id = ?",
            (timezone, user_id)
        )
        await db.commit()

async def get_user_timezone(user_id: int) -> str:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            "SELECT timezone FROM users WHERE user_id = ?",
            (user_id,)
        )
        row = await cursor.fetchone()
        return row[0] if row and row[0] else "Europe/Moscow"

# Функция для сохранения диалога
async def save_conversation(user_id: int, role: str, content: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('''
            INSERT INTO conversations (user_id, role, content, is_active)
            VALUES (?, ?, ?, 1)
        ''', (user_id, role, content))
        await db.commit()

async def clear_completed_tasks(user_id: int):
    """
    Удаляет все выполненные задачи пользователя (completed = 1),
    независимо от даты.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "DELETE FROM todo_tasks WHERE user_id = ? AND completed = 1",
            (user_id,)
        )
        await db.commit()


async def get_todo_tasks_for_today(user_id: int, user_timezone: str = "UTC") -> List[Dict]:
    """
    Возвращает:
    - все невыполненные
    - и выполненные сегодня по ЛОКАЛЬНОЙ дате пользователя
    """
    try:
        tz = ZoneInfo(user_timezone or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    now_local = datetime.now(tz)
    start_local = now_local.replace(hour=0, minute=0, second=0, microsecond=0)
    start_utc_ts = int(start_local.astimezone(ZoneInfo("UTC")).timestamp())

    query = """
    SELECT id, text, created_at, completed, completed_at, remind_at_local, remind_at_ts, reminded
    FROM todo_tasks
    WHERE user_id = ?
      AND (
          completed = 0
          OR (completed = 1 AND completed_at_ts IS NOT NULL AND completed_at_ts >= ?)
      )
    ORDER BY
        completed ASC,                                -- сначала невыполненные
        CASE WHEN remind_at_ts IS NULL THEN 1 ELSE 0 END,
        remind_at_ts ASC,
        created_at ASC
    """

    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("PRAGMA busy_timeout=30000;")
        cursor = await db.execute(query, (user_id, start_utc_ts))
        rows = await cursor.fetchall()
        await cursor.close()

    return [dict(r) for r in rows]

# Функция для добавления в список задач
async def add_todo_task(
    user_id: int,
    text: str,
    remind_at_utc: str | None = None,
    remind_at_local: str | None = None,
    remind_at_ts: int | None = None
) -> int:
    async with aiosqlite.connect(DB_PATH) as db:
        logging.info(f"[db:add_todo_task] DB_PATH={os.path.abspath(DB_PATH)} inserting user_id={user_id} text={text!r} remind_at_ts={remind_at_ts!r}")
        notify_at_ts = remind_at_ts
        notify_at_local = remind_at_local
        notify_offset_min = 0
        cursor = await db.execute(
            """
            INSERT INTO todo_tasks (
                user_id, text,
                remind_at_utc, remind_at_local, remind_at_ts,
                notify_at_ts, notify_at_local, notify_offset_min
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, text, remind_at_utc, remind_at_local, remind_at_ts,
                    notify_at_ts, notify_at_local, notify_offset_min)
        )
        await db.commit()
        task_id = cursor.lastrowid
        await cursor.close()
    logging.info(f"[db:add_todo_task] inserted lastrowid={task_id}")
    return task_id

async def complete_todo_task(task_id: int, user_id: int) -> bool:
    """
    Помечает задачу выполненной.
    Возвращает True, если задача обновлена.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        now_utc = datetime.now(timezone.utc)
        cursor = await db.execute(
            """
            UPDATE todo_tasks
            SET completed = 1,
                completed_at = ?,
                completed_at_ts = ?
            WHERE id = ? AND user_id = ? AND completed = 0
            """,
            (now_utc.isoformat(), int(now_utc.timestamp()), task_id, user_id)
        )
        await db.commit()
        updated = cursor.rowcount
        await cursor.close()

    return updated > 0

# Функция для получения истории диалога
async def get_conversation_history(user_id: int, limit: int = 99):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('''
            SELECT role, content, timestamp 
            FROM conversations 
            WHERE user_id = ? AND is_active = 1
            ORDER BY timestamp ASC 
            LIMIT ?
        ''', (user_id, limit))
        rows = await cursor.fetchall()
        return [{'role': row[0], 'content': row[1], 'timestamp': row[2]} for row in rows]

async def register_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        # Проверяем, существует ли пользователь
        cursor = await db.execute('SELECT * FROM users WHERE user_id = ?', (user_id,))
        existing = await cursor.fetchone()
        if not existing:
            await db.execute('''
                INSERT INTO users (user_id, username, first_name, registration_date)
                VALUES (?, ?, ?, ?)
            ''', (user_id, username, first_name, datetime.utcnow()))
            await db.commit()

async def delete_conversation_context(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE conversations SET is_active = 0 WHERE user_id = ?', (user_id,))
        await db.commit()

async def is_vip(user_id: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT is_vip FROM users WHERE user_id = ?', (user_id,))
        row = await cursor.fetchone()
        return row[0] == 1 if row else False

async def set_vip(user_id: int, is_vip: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute('UPDATE users SET is_vip = ? WHERE user_id = ?', (is_vip, user_id))
        await db.commit()

async def get_vip_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT user_id, username, first_name FROM users WHERE is_vip = 1')
        rows = await cursor.fetchall()
        return rows


async def check_request_limit(user_id: int) -> tuple:
    """Проверяет лимит запросов пользователя. Возвращает (не превышен ли лимит, использовано запросов, лимит)"""
    if await is_vip(user_id):
        return True, 0, "Unlimited"
    
    user_tz = await get_user_timezone(user_id)
    try:
        tz = ZoneInfo(user_tz or "UTC")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    today = datetime.now(tz).date().isoformat() 

    async with aiosqlite.connect(DB_PATH) as db:
        # Получаем количество запросов за сегодня
        cursor = await db.execute(
            'SELECT request_count FROM user_requests WHERE user_id = ? AND request_date = ?',
            (user_id, today)
        )
        result = await cursor.fetchone()

        if result:
            request_count = result[0]
            if request_count >= MAX_REQUESTS_PER_DAY:
                return False, request_count, MAX_REQUESTS_PER_DAY
            else:
                return True, request_count, MAX_REQUESTS_PER_DAY
        else:
            # Если записей нет, значит лимит не превышен
            return True, 0, MAX_REQUESTS_PER_DAY


async def increment_request_count(user_id: int):
    """Увеличивает счетчик запросов пользователя на 1"""
    if await is_vip(user_id):
        return

    user_tz = await get_user_timezone(user_id)
    try:
        tz = ZoneInfo(user_tz or "UTC")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")
    today = datetime.now(tz).date().isoformat()

    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=30000;")
        await db.execute(
            """
            INSERT INTO user_requests (user_id, request_date, request_count)
            VALUES (?, ?, 1)
            ON CONFLICT(user_id, request_date)
            DO UPDATE SET request_count = request_count + 1
            """,
            (user_id, today),
        )
        await db.commit()

async def get_total_requests(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT SUM(request_count) FROM user_requests WHERE user_id = ?', (user_id,))
        result = await cursor.fetchone()
        return result[0] or 0

async def get_total_users():
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT COUNT(*) FROM users')
        result = await cursor.fetchone()
        return result[0]

async def get_today_users():
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT username, first_name FROM users WHERE DATE(registration_date) = ?',
            (today,)
        )
        rows = await cursor.fetchall()
        return rows

async def get_today_total_requests():
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT SUM(request_count) FROM user_requests WHERE request_date = ?',
            (today,)
        )
        result = await cursor.fetchone()
        return result[0] or 0

async def get_today_unique_users_with_requests():
    today = date.today().isoformat()
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute(
            'SELECT COUNT(DISTINCT user_id) FROM user_requests WHERE request_date = ? AND request_count > 0',
            (today,)
        )
        result = await cursor.fetchone()
        return result[0]

async def set_task_notify_time(
    task_id: int,
    user_id: int,
    notify_at_ts: int,
    notify_at_local: str,
    notify_offset_min: int = 0,
):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            """
            UPDATE todo_tasks
            SET notify_at_ts = ?,
                notify_at_local = ?,
                notify_offset_min = ?
            WHERE id = ? AND user_id = ?
            """,
            (notify_at_ts, notify_at_local, notify_offset_min, task_id, user_id),
        )
        await db.commit()


async def get_task_remind_at_ts(task_id: int, user_id: int) -> int | None:
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT remind_at_ts
            FROM todo_tasks
            WHERE id = ? AND user_id = ?
            """,
            (task_id, user_id),
        )
        row = await cur.fetchone()
        await cur.close()

    if not row or row["remind_at_ts"] is None:
        return None

    return int(row["remind_at_ts"])

async def get_tasks_context(user_id: int, limit: int = 30) -> list[dict]:
    """
    Возвращает последние задачи пользователя (и невыполненные, и выполненные),
    чтобы давать ИИ контекст.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            """
            SELECT id, text, completed, remind_at_local, notify_at_local, notify_offset_min, reminded, created_at
            FROM todo_tasks
            WHERE user_id = ?
            ORDER BY id DESC
            LIMIT ?
            """,
            (user_id, limit),
        )
        rows = await cur.fetchall()
        await cur.close()

    # Разворачиваем, чтобы в контексте было “сначала старые -> потом новые”
    rows = list(reversed(rows))
    return [dict(r) for r in rows]