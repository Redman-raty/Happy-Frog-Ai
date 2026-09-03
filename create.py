import asyncio
import aiosqlite
import os
from config import DB_PATH


async def init_db():
    try:
        async with aiosqlite.connect(DB_PATH) as db:
            # Таблица пользователей
            await db.execute('''
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER UNIQUE,
                    username TEXT,
                    first_name TEXT,
                    registration_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                    total_requests INTEGER DEFAULT 0,
                    is_vip INTEGER DEFAULT 0
                )
            ''')

            # Таблица запросов пользователей
            await db.execute('''
                CREATE TABLE IF NOT EXISTS user_requests (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    request_date DATE,
                    request_count INTEGER DEFAULT 0,
                    UNIQUE(user_id, request_date),
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')

            # Таблица для сохранения диалогов
            await db.execute('''
                CREATE TABLE IF NOT EXISTS conversations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    role TEXT,
                    content TEXT,
                    is_active INTEGER DEFAULT 1,
                    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id)
                )
            ''')
            await db.commit()
            print(f"✅ База данных успешно инициализирована по пути: {DB_PATH}")

            # Показываем структуру таблиц
            cursor = await db.execute("SELECT name FROM sqlite_master WHERE type='table';")
            tables = await cursor.fetchall()
            print("📊 Созданные таблицы:")
            for table in tables:
                print(f"  - {table[0]}")

    except Exception as e:
        print(f"❌ Ошибка инициализации БД: {e}")
        raise


if __name__ == "__main__":
    # Создаем директорию для БД если её нет
    db_dir = os.path.dirname(DB_PATH)
    if db_dir and not os.path.exists(db_dir):
        os.makedirs(db_dir)
        print(f"📁 Создана директория: {db_dir}")

    asyncio.run(init_db())