# bot.py
import asyncio
import logging
import sys
import os

from datetime import datetime, timezone
import aiosqlite
from config import DB_PATH
from aiogram import Bot
from aiogram import types
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import (
    TelegramForbiddenError,
    TelegramBadRequest,
    TelegramNetworkError,
)
from config import BOT_TOKEN
from database import init_db
from handlers import dp
from utils import close_browser

# Настройка логирования
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger(__name__)

reminder_task: asyncio.Task | None = None


# Функции запуска/остановки
async def on_startup(bot: Bot):
    logger.info("Бот запущен.")


async def on_shutdown(bot: Bot):
    global reminder_task
    if reminder_task:
        reminder_task.cancel()
        try:
            await reminder_task
        except asyncio.CancelledError:
            pass
        reminder_task = None
    await close_browser()
    await bot.session.close()
    logger.info("Бот остановлен.")


async def main():
    # Инициализация бота
    bot = Bot(
        token=BOT_TOKEN,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )

    # Глобальный обработчик ошибок aiogram 3
    @dp.errors()
    async def error_handler(event):
        """
        event – это объект ErrorEvent.
        Через event.exception получаем саму ошибку,
        через event.update – апдейт, на котором она случилась.
        """
        error = event.exception

        if isinstance(error, (TelegramForbiddenError, TelegramBadRequest)):
            error_msg = str(error).lower()
            if (
                "chat not found" in error_msg
                or "blocked" in error_msg
                or "forbidden" in error_msg
            ):
                logger.warning(
                    f"Чат недоступен (пользователь вышел/заблокировал бота): {error}"
                )
                return True  # ошибка обработана, дальше не пробрасываем

        if isinstance(error, TelegramNetworkError):
            logger.warning(f"Сетевой сбой Telegram: {error}")
            return True  # не валим всё приложение из-за сетевого глюка

        logger.exception(f"Необработанная ошибка: {error}")
        return True

    logger.info("Инициализация БД...")
    await init_db()

    global reminder_task
    reminder_task = asyncio.create_task(reminder_worker(bot), name="reminder_worker")

    def _log_task_result(t: asyncio.Task):
        try:
            t.result()
        except asyncio.CancelledError:
            logger.info("[reminder_worker] cancelled")
        except Exception:
            logger.exception("[reminder_worker] crashed!")

    reminder_task.add_done_callback(_log_task_result)
    logger.info("[main] reminder_worker task created")



    logger.info("Запуск polling...")
    await dp.start_polling(
        bot,
        on_startup=on_startup,
        on_shutdown=on_shutdown,
        allowed_updates=dp.resolve_used_update_types(),
        skip_updates=True,
    )


async def reminder_worker(bot: Bot):
    log = logging.getLogger("reminder_worker")

    while True:
        now_ts = int(datetime.now(timezone.utc).timestamp())

        due = []
        try:
            async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                db.row_factory = aiosqlite.Row
                await db.execute("PRAGMA busy_timeout=30000;")

                cur = await db.execute(
                    """
                    SELECT id, user_id, text, notify_at_ts
                    FROM todo_tasks
                    WHERE completed = 0
                      AND reminded = 0
                      AND notify_at_ts IS NOT NULL
                      AND notify_at_ts <= ?
                    ORDER BY notify_at_ts ASC
                    LIMIT 50
                    """,
                    (now_ts,),
                )
                due = await cur.fetchall()
                await cur.close()

        except Exception as e:
            log.exception(f"[reminder_worker] DB select error: {e!r}")
            await asyncio.sleep(5)
            continue

        if not due:
            await asyncio.sleep(10)
            continue

        # Какие задачи можно пометить reminded=1
        to_mark_reminded: list[int] = []

        for r in due:
            task_id = int(r["id"])
            user_id = int(r["user_id"])
            text = r["text"]

            try:
                await bot.send_message(
                    chat_id=user_id,
                    text=f"⏰ Напоминание по задаче:\n\n• {text}",
                )
                to_mark_reminded.append(task_id)

            except (TelegramForbiddenError, TelegramBadRequest) as e:
                # чат недоступен — помечаем, чтобы не долбить дальше
                log.warning(f"[send] chat unavailable task_id={task_id}: {e!r}")
                to_mark_reminded.append(task_id)

            except TelegramNetworkError as e:
                # сеть — НЕ помечаем, чтобы повторить
                log.warning(f"[send] network error task_id={task_id}: {e!r}")

            except Exception as e:
                # другое — НЕ помечаем, чтобы повторить/разобраться
                log.exception(f"[send] unexpected error task_id={task_id}: {e!r}")

        # Один батч-апдейт вместо N подключений
        if to_mark_reminded:
            try:
                async with aiosqlite.connect(DB_PATH, timeout=30) as db:
                    await db.execute("PRAGMA busy_timeout=30000;")
                    await db.executemany(
                        "UPDATE todo_tasks SET reminded = 1 WHERE id = ?",
                        [(tid,) for tid in to_mark_reminded],
                    )
                    await db.commit()
            except Exception as e:
                log.exception(f"[reminder_worker] DB update error: {e!r}")

        await asyncio.sleep(10)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен пользователем.")