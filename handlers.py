import os
import tempfile
import asyncio
import logging
import html


from config import DB_PATH
import aiosqlite
from database import set_task_notify_time, get_user_timezone
from zoneinfo import ZoneInfo
from datetime import datetime, timezone, timedelta
from aiogram.filters import Command
from database import set_user_timezone
from typing import List, Dict, Any
from aiogram import Dispatcher, F, types, Bot
from aiogram.filters import CommandStart, Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton,
    FSInputFile, ReplyKeyboardRemove
)

# Внешние зависимости (твои модули)
from database import (
    register_user, check_request_limit, increment_request_count, save_conversation,
    get_conversation_history, get_total_users, get_today_users, get_today_total_requests,
    get_today_unique_users_with_requests, delete_conversation_context, get_total_requests,
    set_vip, is_vip, get_vip_users, add_todo_task, get_todo_tasks_for_today,complete_todo_task, clear_completed_tasks,get_user_timezone,
    get_task_remind_at_ts,get_tasks_context
)
from utils import (send_to_ai, transcribe_audio, export_table_to_excel, get_db_file, broadcast_message,
                    generate_tasks_table_image,extract_todo_tasks_from_text,complete_tasks_by_descriptions,
                    normalize_remind_at, format_tasks_context)
from config import PROMPT_FOR_AI, ADMIN_IDS, MAX_MESSAGE_LENGTH, MAX_PROMPT_LENGTH, MAX_TASKS_PER_IMAGE

# Логирование
logging.basicConfig(level=logging.INFO)

# Инициализируем Dispatcher
dp = Dispatcher()

# --- debounce обновления списка задач (по сообщению с таблицей) ---
_tasks_worker: dict[tuple[int, int], asyncio.Task] = {}

# накопление кликов (по сообщению таблицы)
_pending_completions: dict[tuple[int, int], set[int]] = {}
_last_click_ts: dict[tuple[int, int], float] = {}

# Клавиатура с основными действиями
main_menu = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Статистика")],
        [KeyboardButton(text="📋 Мои задачи")]
    ],
    resize_keyboard=True
)

# Inline-клавиатура для раздела "Статистика" пользователя
stats_inline_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Очистить выполненные задачи", callback_data="clear_completed_tasks")],
        [InlineKeyboardButton(text="Очистить контекст", callback_data="clear_context")]
    ]
)

# Inline-клавиатура для админ-панели
admin_menu = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text='Статистика', callback_data='admin_stats')],
        [InlineKeyboardButton(text='Рассылка новости', callback_data='admin_broadcast')],
        [InlineKeyboardButton(text='Получить файл с пользователями', callback_data='admin_export_users')],
        [InlineKeyboardButton(text='Получить файл переписок', callback_data='admin_export_conversations')],
        [InlineKeyboardButton(text='Получить счетчик запросов', callback_data='admin_export_requests')],
        [InlineKeyboardButton(text='Получить всю базу данных', callback_data='admin_export_db')],
        [InlineKeyboardButton(text='Добавить VIP', callback_data='admin_add_vip')],
        [InlineKeyboardButton(text='Удалить VIP', callback_data='admin_remove_vip')]
    ]
)

# Inline-клавиатура для подтверждения рассылки
confirm_keyboard = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Подтвердить", callback_data="confirm_broadcast")],
        [InlineKeyboardButton(text="Отменить", callback_data="cancel_broadcast")]
    ]
)

# Inline-клавиатура "Без фотографии"
no_photo_inline_kb = InlineKeyboardMarkup(
    inline_keyboard=[
        [InlineKeyboardButton(text="Без фотографии", callback_data="no_photo")]
    ]
)


# Функция безопасной отправки (не падает при ошибке)
async def safe_send(target: types.Message | types.CallbackQuery | types.User, text: str,
                    reply_markup=None, parse_mode="HTML"):
    """
    target: ожидается объект, у которого есть .answer или .message.answer
    Для простоты используем duck-typing: если у объекта есть attribute 'message' — пробуем .message.answer,
    иначе пробуем .answer.
    """
    try:
        if isinstance(target, types.CallbackQuery):
            return await target.message.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        elif isinstance(target, types.Message):
            return await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
        else:
            # fallback — попытка вызвать .answer
            return await target.answer(text, parse_mode=parse_mode, reply_markup=reply_markup)
    except Exception as e:
        logging.error(f"Ошибка отправки сообщения: {e}")
        return None


# Функция безопасного удаления сообщения
async def safe_delete(msg):
    if msg:
        try:
            await msg.delete()
        except Exception as e:
            logging.error(f"Ошибка удаления сообщения: {e}")


# Отправка длинных сообщений с защитой
async def send_long_message(message: types.Message, text: str, reply_markup=None, parse_mode="HTML"):
    parts = []
    while len(text) > MAX_MESSAGE_LENGTH:
        split_index = text[:MAX_MESSAGE_LENGTH].rfind('\n')
        if split_index == -1:
            split_index = text[:MAX_MESSAGE_LENGTH].rfind(' ')
        if split_index == -1:
            split_index = MAX_MESSAGE_LENGTH
        parts.append(text[:split_index])
        text = text[split_index:].lstrip()
    parts.append(text)

    for i, part in enumerate(parts):
        try:
            if i == len(parts) - 1:
                await message.answer(part, parse_mode=parse_mode, reply_markup=reply_markup)
            else:
                await message.answer(part, parse_mode=parse_mode)
            await asyncio.sleep(0.2)
        except Exception as e:
            logging.error(f"Ошибка отправки части сообщения: {e}")


# Состояния
class QueryStates(StatesGroup):
    processing_voice = State()
    processing_query = State()


class AdminStates(StatesGroup):
    waiting_news_text = State()
    waiting_news_photo = State()
    confirming_broadcast = State()
    waiting_add_vip = State()
    waiting_remove_vip = State()


# -------------------
# /start -> приветствие с фото + сброс состояния
# -------------------
@dp.message(CommandStart())
async def start_handler(message: types.Message, state: FSMContext):
    # Сбрасываем состояние и данные
    try:
        await state.clear()
    except Exception:
        pass

    # Регистрируем пользователя (если требуется)
    try:
        await register_user(message.from_user.id, message.from_user.username, message.from_user.first_name)
    except Exception as e:
        logging.error(f"Ошибка регистрации пользователя: {e}")

    photo_path = "static/frog.jpg"  # помести сюда своё фото
    caption = (
        "👋 <b>Привет!</b>\n\n"
        "Я бот, который умеет отвечать на вопросы и расшифровывать голосовые сообщения (voice и кружочки — video_note).\n\n"
        "📩 Отправь текст или голосовое — и я помогу!\n\n"
        "Используй кнопки внизу для управления."
    )

    try:
        # Отправляем фото с подписью (если файла нет — отправляем только текст)
        if os.path.exists(photo_path):
            await message.answer_photo(FSInputFile(photo_path), caption=caption, parse_mode="HTML", reply_markup=main_menu)
        else:
            await message.answer(caption, parse_mode="HTML", reply_markup=main_menu)
    except Exception as e:
        logging.error(f"Ошибка отправки фото при старте: {e}")
        await message.answer(caption, parse_mode="HTML", reply_markup=main_menu)


# -------------------
# Кнопка "Очистить контекст"
# -------------------
@dp.message(F.text == "Очистить контекст")
async def clear_context_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in [QueryStates.processing_voice, QueryStates.processing_query]:
        await safe_send(message, "Подождите, идет обработка другого запроса...", reply_markup=main_menu)
        return

    try:
        await delete_conversation_context(message.from_user.id)
    except Exception as e:
        logging.error(f"Ошибка при удалении контекста в БД: {e}")
    await safe_send(message, "Контекст диалога очищен.", reply_markup=main_menu)


# -------------------
# Кнопка "Статистика"
# -------------------
@dp.message(F.text == "Статистика")
async def user_stats_handler(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if current_state in [QueryStates.processing_voice, QueryStates.processing_query]:
        await safe_send(message, "Подождите, идет обработка другого запроса...", reply_markup=main_menu)
        return

    user_id = message.from_user.id
    can_request, used_today, limit_today = await check_request_limit(user_id)
    total_requests = await get_total_requests(user_id)
    is_vip_user = await is_vip(user_id)

    text = "Ваша статистика:\n\n"
    text += f"ID: <code>{user_id}</code>\n"
    text += f"Всего запросов: {total_requests}\n"
    text += f"Сегодня: {used_today}\n"

    if is_vip_user:
        text += "Статус: Премиум\n"
        text += "Осталось: Unlimited"
    else:
        remained = limit_today - used_today if isinstance(limit_today, int) else 0
        text += f"Осталось: {remained}\n"

    await safe_send(message, text, reply_markup=stats_inline_kb)

# -------------------
# Админ-панель команда
# -------------------
@dp.message(Command('admin'))
async def admin_handler(message: types.Message, state: FSMContext):
    if str(message.from_user.id) not in ADMIN_IDS:
        return
    await safe_send(message, "Админ-панель:", reply_markup=admin_menu)


# -------------------
# Статистика для админа
# -------------------
@dp.message(Command('stats'))
@dp.callback_query(F.data == 'admin_stats')
async def stats_handler(event: types.Message | types.CallbackQuery, state: FSMContext):
    if isinstance(event, types.CallbackQuery):
        if str(event.from_user.id) not in ADMIN_IDS:
            return
        message = event.message
    else:
        if str(event.from_user.id) not in ADMIN_IDS:
            return
        message = event

    total_users = await get_total_users()
    today_users = await get_today_users()
    today_requests = await get_today_total_requests()
    unique_users = await get_today_unique_users_with_requests()
    avg_requests = today_requests / unique_users if unique_users > 0 else 0

    today_users_list = "\n".join([f"{i+1}. {row[0]} {row[1]}" for i, row in enumerate(today_users)]) if today_users else "Нет новых"
    text = f"Общее кол-во зарегистрированных пользователей: {total_users}\n\n"
    text += f"Список пользователей зарегистрированных сегодня:\n{today_users_list}\n\n"
    text += f"Кол-во запросов сделанных за сегодня суммарно: {today_requests}\n"
    text += f"Уникальное кол-во пользователей сделавших сегодня запросы: {unique_users}\n"
    text += f"Сколько в среднем запросов сделал один юзер: {avg_requests:.2f}"

    await send_long_message(message, text, reply_markup=admin_menu)
    if isinstance(event, types.CallbackQuery):
        await event.answer()


# -------------------
# Экспорт файлов (админ)
# -------------------
@dp.callback_query(F.data.in_(['admin_export_users', 'admin_export_conversations', 'admin_export_requests', 'admin_export_db']))
async def export_handler(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) not in ADMIN_IDS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return

    mapping = {
        'admin_export_users': ('users', 'Пользователи'),
        'admin_export_conversations': ('conversations', 'Переписки'),
        'admin_export_requests': ('user_requests', 'Запросы'),
        'admin_export_db': (None, 'База данных')
    }
    action = callback.data
    table, title = mapping[action]

    try:
        if table:
            file_path = await export_table_to_excel(table)
            await callback.message.answer_document(FSInputFile(file_path), caption=f"{title}.xlsx", reply_markup=admin_menu)
            os.unlink(file_path)
        else:
            file_path = await get_db_file()
            await callback.message.answer_document(FSInputFile(file_path), caption="Полная база данных", reply_markup=admin_menu)
    except Exception as e:
        logging.error(f"Ошибка экспорта: {e}")
        await safe_send(callback.message, "Ошибка при экспорте файла.", reply_markup=admin_menu)
    await callback.answer()


# -------------------
# Рассылка (админ)
# -------------------
@dp.callback_query(F.data == 'admin_broadcast')
async def start_broadcast_handler(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) not in ADMIN_IDS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    await safe_send(callback.message, "Введите текст новости:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminStates.waiting_news_text)
    await callback.answer()


@dp.message(AdminStates.waiting_news_text)
async def get_news_text_handler(message: types.Message, state: FSMContext):
    await state.update_data(news_text=message.text)
    await safe_send(message, "Отправьте фотографию или выберите 'Без фотографии':", reply_markup=no_photo_inline_kb)
    await state.set_state(AdminStates.waiting_news_photo)


@dp.message(AdminStates.waiting_news_photo, F.content_type == 'photo')
@dp.callback_query(AdminStates.waiting_news_photo, F.data == 'no_photo')
async def get_news_photo_handler(event: types.Message | types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    if isinstance(event, types.CallbackQuery):
        photo = None
        await event.message.edit_reply_markup(reply_markup=None)
        await event.answer()
        message = event.message
    else:
        photo = event.photo[-1].file_id
        message = event

    await state.update_data(news_photo=photo)
    text = data['news_text']
    if photo:
        await message.answer_photo(photo, caption=text, reply_markup=confirm_keyboard)
    else:
        await safe_send(message, text, reply_markup=confirm_keyboard)
    await state.set_state(AdminStates.confirming_broadcast)


@dp.callback_query(F.data == 'confirm_broadcast')
async def confirm_broadcast_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Рассылка начата.")
    await callback.message.edit_reply_markup(reply_markup=None)
    data = await state.get_data()
    text = data['news_text']
    photo = data.get('news_photo')
    sent = await broadcast_message(callback.bot, text, photo)
    await safe_send(callback.message, f"Рассылка завершена. Отправлено {sent} пользователям.", reply_markup=admin_menu)
    await state.clear()


@dp.callback_query(F.data == 'cancel_broadcast')
async def cancel_broadcast_callback(callback: types.CallbackQuery, state: FSMContext):
    await callback.answer("Рассылка отменена.")
    await callback.message.edit_reply_markup(reply_markup=None)
    await safe_send(callback.message, "Рассылка отменена.", reply_markup=admin_menu)
    await state.clear()


# -------------------
# Добавление VIP
# -------------------
@dp.callback_query(F.data == 'admin_add_vip')
async def start_add_vip_handler(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) not in ADMIN_IDS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    await safe_send(callback.message, "Введите Telegram ID пользователя для добавления VIP:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminStates.waiting_add_vip)
    await callback.answer()


@dp.message(AdminStates.waiting_add_vip)
async def add_vip_handler(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await set_vip(user_id, 1)
        await safe_send(message, f"Пользователь {user_id} добавлен в VIP.", reply_markup=admin_menu)
    except ValueError:
        await safe_send(message, "Неверный ID. Введите число.", reply_markup=admin_menu)
    await state.clear()


# -------------------
# Удаление VIP
# -------------------
@dp.callback_query(F.data == 'admin_remove_vip')
async def start_remove_vip_handler(callback: types.CallbackQuery, state: FSMContext):
    if str(callback.from_user.id) not in ADMIN_IDS:
        await callback.answer("Доступ запрещен.", show_alert=True)
        return
    vip_users = await get_vip_users()
    vip_list = "\n".join([f"{row[0]} - {row[1]} {row[2]}" for row in vip_users]) if vip_users else "Нет VIP пользователей."
    await safe_send(callback.message, f"Текущие VIP пользователи:\n{vip_list}\n\nВведите Telegram ID пользователя для удаления VIP:", reply_markup=ReplyKeyboardRemove())
    await state.set_state(AdminStates.waiting_remove_vip)
    await callback.answer()


@dp.message(AdminStates.waiting_remove_vip)
async def remove_vip_handler(message: types.Message, state: FSMContext):
    try:
        user_id = int(message.text)
        await set_vip(user_id, 0)
        await safe_send(message, f"Пользователь {user_id} удален из VIP.", reply_markup=admin_menu)
    except ValueError:
        await safe_send(message, "Неверный ID. Введите число.", reply_markup=admin_menu)
    await state.clear()

# -------------------
# Основной хендлер: текст, voice, video_note
# -------------------
# Добавил 'video_note' в фильтр
@dp.message(
    StateFilter(None),
    F.content_type.in_({'text', 'voice', 'video_note'}),
    ~Command('admin'),
    ~CommandStart(),
    ~F.text.in_({"Статистика", "Очистить контекст", "📋 Мои задачи"})
)
async def handle_user_query(message: types.Message, state: FSMContext):

    current_state = await state.get_state()
    if current_state in [QueryStates.processing_voice, QueryStates.processing_query]:
        await safe_send(message, "Подождите, идет обработка другого запроса...", reply_markup=main_menu)
        return

    user_id = message.from_user.id
    can, used, limit = await check_request_limit(user_id)
    if not can:
        await safe_send(
            message,
            f"Вы исчерпали лимит запросов {used}/{limit}",
            reply_markup=main_menu
        )
        return

    # ---------- ГОЛОС ----------
    if message.content_type in ('voice', 'video_note'):
        await state.set_state(QueryStates.processing_voice)
        msg = await safe_send(message, "Обрабатываю аудио...")

        user_id = message.from_user.id

        # 1) Скачиваем файл из Telegram во временный файл
        file_id = message.voice.file_id if message.content_type == 'voice' else message.video_note.file_id
        file = await message.bot.get_file(file_id)

        with tempfile.NamedTemporaryFile(delete=False, suffix='.ogg') as temp_audio:
            audio_path = temp_audio.name

        await message.bot.download_file(file.file_path, audio_path)

        # 2) Распознаём
        transcription = await transcribe_audio(audio_path)

        try:
            os.unlink(audio_path)
        except Exception:
            pass

        await safe_delete(msg)

        if transcription.startswith("❌"):
            await safe_send(message, transcription, reply_markup=main_menu)
            await state.clear()
            return

        # 3) Пробуем выделить задачи из текста голосового
        user_tz = await get_user_timezone(user_id)
        todo_items, done_items = await extract_todo_tasks_from_text(transcription, user_tz)

        # дедуп todo
        seen = set()
        todo_clean = []
        for it in todo_items:
            key = (it.get("text") or "").strip().lower()
            if key and key not in seen:
                seen.add(key)
                todo_clean.append(it)
        todo_items = todo_clean

        # дедуп done
        seen_done = set()
        done_clean = []
        for it in done_items:
            key = (it.get("text") or "").strip().lower()
            if key and key not in seen_done:
                seen_done.add(key)
                done_clean.append(it)
        done_items = done_clean

        # 4) Если задач нет — это обычный вопрос к ИИ (как у тебя было)
        if not todo_items and not done_items:
            await save_conversation(user_id, "user", transcription)
            await state.set_state(QueryStates.processing_query)
            gen = await safe_send(message, "Формулирую ответ...")

            try:
                tasks_ctx = await get_tasks_context(user_id, limit=30)
                tasks_block = format_tasks_context(tasks_ctx)

                prompt_base = (
                    f"{PROMPT_FOR_AI}\n\n"
                    "TASK CONTEXT (user todo list; may be relevant):\n"
                    f"{tasks_block}\n\n"
                    "CONVERSATION HISTORY:\n\n"
                    )
                history = await get_conversation_history(user_id, 99)

                dialog = ""
                for h in history:
                    role = "User" if h['role'] == 'user' else "Assistant"
                    dialog += f"{role}: {h['content']}\n\n"

                full_prompt = f"{prompt_base}{dialog}CURRENT USER QUESTION: {transcription}\n\nДай развернутый ответ."
                resp = await send_to_ai(full_prompt)

                await save_conversation(user_id, "assistant", resp)
                await increment_request_count(user_id)

                await safe_delete(gen)
                await send_long_message(message, resp, reply_markup=main_menu)

            except Exception as e:
                await safe_send(message, f"Ошибка: {e}", reply_markup=main_menu)
            finally:
                await state.clear()

            return

        # 5) Есть задачи/выполненное — обрабатываем как текстовые
        added = []  # сюда кладём ТОЛЬКО задачи БЕЗ времени (по твоему UX)
        for item in todo_items:
            task_text = (item.get("text") or "").strip()
            if not task_text:
                continue

            remind_at_utc = None
            remind_at_local = None
            remind_at_ts = None

            raw_remind = item.get("remind_at")
            logging.info(f"[task_parse:voice] user_id={user_id} raw_remind_at={raw_remind!r} user_tz={user_tz!r}")

            if raw_remind:
                # ✅ та же логика, что и в текстовых: если время в прошлом — НЕ добавляем
                utc_dt, local_str, err = normalize_remind_at(raw_remind, user_tz, transcription)
                if err:
                    await safe_send(message, err, reply_markup=main_menu)
                    continue

                remind_at_utc = utc_dt.isoformat()
                remind_at_local = local_str
                remind_at_ts = int(utc_dt.timestamp())

            task_id = await add_todo_task(
                user_id=user_id,
                text=task_text,
                remind_at_utc=remind_at_utc,
                remind_at_local=remind_at_local,
                remind_at_ts=remind_at_ts
            )

            # ✅ Если у задачи ЕСТЬ время — показываем ТОЛЬКО "предупредить за"
            if remind_at_ts is not None and remind_at_local:
                await message.answer(
                    f"⏰ Событие: <b>{html.escape(task_text)}</b>\n"
                    f"🕒 Время события: <b>{html.escape(remind_at_local)}</b>\n\n"
                    f"<b>Предупредить за:</b>",
                    reply_markup=build_pre_remind_kb(task_id),
                    parse_mode="HTML",
                )
            else:
                # ✅ Если времени нет — добавляем в общий список
                added.append((task_text, None))

        # 6) done_items → отмечаем выполненными
        completed = []
        if done_items:
            completed = await complete_tasks_by_descriptions(
                user_id,
                [d["text"] for d in done_items if d.get("text")]
            )

        # 7) Итоговые сообщения: только для задач без времени + completed
        parts = []

        if added:
            lines = [f"• {t}" for t, _ in added]
            parts.append("📋 Добавил задачи:\n" + "\n".join(lines))

        if completed:
            parts.append("✅ Отметил выполнёнными:\n" + "\n".join(f"• {t}" for t in completed))
        elif done_items:
            parts.append("🤔 Не нашёл соответствующих задач для отметки.")

        if parts:
            await safe_send(message, "\n\n".join(parts), reply_markup=main_menu)

        # считаем это одним запросом
        await increment_request_count(user_id)
        await state.clear()
        return

    # ---------- ТЕКСТ ----------

    text = message.text.strip()

    # Снимаем кнопку расшифровки если была
    data = await state.get_data()
    last_msg_id = data.get("last_transcription_msg_id")
    if last_msg_id:
        try:
            await message.bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=last_msg_id,
                reply_markup=None
            )
        except:
            pass
        await state.update_data(last_transcription_msg_id=None, transcription=None)

     # 1. ВСЕГДА пробуем выделить задачи (независимо от интента)
    user_tz = await get_user_timezone(user_id)
    todo_items, done_items = await extract_todo_tasks_from_text(text, user_tz)

    # дедуп по text
    seen = set()
    todo_items_clean = []
    for it in todo_items:
        key = (it.get("text") or "").strip().lower()
        if key and key not in seen:
            seen.add(key)
            todo_items_clean.append(it)
    todo_items = todo_items_clean

    seen_done = set()
    done_items_clean = []
    for it in done_items:
        key = (it.get("text") or "").strip().lower()
        if key and key not in seen_done:
            seen_done.add(key)
            done_items_clean.append(it)
    done_items = done_items_clean

    # 2. Если есть хоть что-то — работаем КАК С ЗАДАЧАМИ
    if todo_items or done_items:
        added = []  # только задачи БЕЗ времени (чтобы вывести одним списком)

        # --- добавляем todo ---
        for item in todo_items:
            task_text = (item.get("text") or "").strip()
            if not task_text:
                continue

            remind_at_utc = None
            remind_at_local = None
            remind_at_ts = None

            raw_remind = item.get("remind_at")
            logging.info(f"[task_parse:text] user_id={user_id} raw_remind_at={raw_remind!r} user_tz={user_tz!r}")

            if raw_remind:
                utc_dt, local_str, err = normalize_remind_at(raw_remind, user_tz, text)
                if err:
                    await safe_send(message, err, reply_markup=main_menu)
                    continue  # ❗ НЕ добавляем задачу, если время прошло/ошибка

                remind_at_utc = utc_dt.isoformat()
                remind_at_local = local_str
                remind_at_ts = int(utc_dt.timestamp())

            task_id = await add_todo_task(
                user_id=user_id,
                text=task_text,
                remind_at_utc=remind_at_utc,
                remind_at_local=remind_at_local,
                remind_at_ts=remind_at_ts,
            )

            # ✅ Если есть время события — показываем "предупредить за" сразу отдельным сообщением
            if remind_at_ts is not None and remind_at_local:
                await message.answer(
                    f"⏰ Событие: <b>{html.escape(task_text)}</b>\n"
                    f"🕒 Время события: <b>{html.escape(remind_at_local)}</b>\n\n"
                    f"<b>Предупредить за:</b>",
                    reply_markup=build_pre_remind_kb(task_id),
                    parse_mode="HTML",
                )
            else:
                added.append(task_text)

        # --- done_items → отмечаем выполненными ---
        completed = []
        if done_items:
            completed = await complete_tasks_by_descriptions(
                user_id,
                [d["text"] for d in done_items if d.get("text")]
            )

        # --- итоговое сообщение ---
        parts = []
        if added:
            parts.append("📋 Добавил задачи:\n" + "\n".join(f"• {t}" for t in added))

        if completed:
            parts.append("✅ Отметил выполнёнными:\n" + "\n".join(f"• {t}" for t in completed))
        elif done_items:
            parts.append("🤔 Не нашёл соответствующих задач для отметки.")

        if parts:
            await safe_send(message, "\n\n".join(parts), reply_markup=main_menu)

        # ✅ ВАЖНО: если мы обработали как задачи — НЕ идём в AI-ветку
        await increment_request_count(user_id)
        return



    # 3. Если задач НЕ нашлось → это вопрос к AI

    await save_conversation(user_id, "user", text)
    await state.set_state(QueryStates.processing_query)

    gen = await safe_send(message, "Формулирую ответ...")

    try:
        tasks_ctx = await get_tasks_context(user_id, limit=30)
        tasks_block = format_tasks_context(tasks_ctx)

        prompt_base = (
            f"{PROMPT_FOR_AI}\n\n"
            "TASK CONTEXT (user todo list; may be relevant):\n"
            f"{tasks_block}\n\n"
            "CONVERSATION HISTORY:\n\n"
        )
        history = await get_conversation_history(user_id, 99)
        dialog = ""
        for h in history:
            role = "User" if h['role'] == 'user' else "Assistant"
            dialog += f"{role}: {h['content']}\n\n"

        full_prompt = f"{prompt_base}{dialog}CURRENT USER QUESTION: {text}\n\nДай развернутый ответ."

        resp = await send_to_ai(full_prompt)

        await save_conversation(user_id, "assistant", resp)
        await increment_request_count(user_id)
        await safe_delete(gen)
        await send_long_message(message, resp, reply_markup=main_menu)

    except Exception as e:
        await safe_send(message, f"Ошибка: {e}", reply_markup=main_menu)
    finally:
        await state.clear()


async def complete_tasks_batch(user_id: int, task_ids: list[int]) -> None:
    """
    Быстро отмечает пачку задач выполненными одним подключением к SQLite.
    Это НАМНОГО быстрее, чем дергать complete_todo_task на каждый клик.
    """
    if not task_ids:
        return

    now_utc = datetime.now(timezone.utc)
    now_iso = now_utc.isoformat()
    now_ts = int(now_utc.timestamp())

    async with aiosqlite.connect(DB_PATH, timeout=30) as db:
        await db.execute("PRAGMA busy_timeout=30000;")

        await db.executemany(
            """
            UPDATE todo_tasks
            SET completed = 1,
                completed_at = ?,
                completed_at_ts = ?
            WHERE id = ?
              AND user_id = ?
              AND completed = 0
            """,
            [(now_iso, now_ts, tid, user_id) for tid in task_ids],
        )
        await db.commit()


async def _tasks_click_worker(bot: Bot, chat_id: int, message_id: int, user_id: int):
    """
    Один воркер на одно сообщение-таблицу.
    Он не отменяется на каждый клик. Он просто смотрит: была ли тишина 2 сек?
    Когда наступила тишина — применяет пачку выполнений и перерисовывает таблицу ОДИН раз.
    """
    key = (chat_id, message_id)
    loop = asyncio.get_running_loop()

    try:
        while True:
            await asyncio.sleep(2)

            last = _last_click_ts.get(key, 0.0)
            # Если за последние 2 секунды был клик — продолжаем ждать
            if loop.time() - last < 2.0:
                continue

            # Тишина наступила → забираем пачку
            task_ids = list(_pending_completions.get(key, set()))
            _pending_completions.pop(key, None)

            # Отмечаем выполненными одним батчем
            if task_ids:
                await complete_tasks_batch(user_id, task_ids)

            # Дальше: твоя логика "нарисовать новую таблицу и удалить старую"
            user_tz = await get_user_timezone(user_id)
            tasks_today = await get_todo_tasks_for_today(user_id, user_tz)

            if not tasks_today:
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except Exception:
                    pass
                await bot.send_message(chat_id, "Все задачи выполнены! 🎉")
                return

            file_path = await generate_tasks_table_image(tasks_today)

            try:
                photo = FSInputFile(file_path)
                kb = build_tasks_inline_kb(tasks_today)

                caption = (
                    "Твои задачи на сегодня 📋\n\n"
                    "Номера на кнопках соответствуют задачам в таблице сверху вниз.\n"
                    "Нажми номер, чтобы отметить задачу выполненной ✅"
                )

                # СНАЧАЛА отправляем новую таблицу
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=photo,
                    caption=caption,
                    reply_markup=kb
                )

                # ПОТОМ удаляем старую
                try:
                    await bot.delete_message(chat_id=chat_id, message_id=message_id)
                except Exception:
                    pass

            finally:
                try:
                    os.unlink(file_path)
                except Exception:
                    pass

            return  # ✅ воркер отработал один раз и завершился

    finally:
        _tasks_worker.pop(key, None)
        _pending_completions.pop(key, None)
        _last_click_ts.pop(key, None)

@dp.callback_query(lambda c: c.data and c.data.startswith("task_"))
async def handle_task_button(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    chat_id = callback.message.chat.id
    msg_id = callback.message.message_id
    key = (chat_id, msg_id)

    # 1) достаём task_id
    try:
        task_id = int(callback.data.split("_")[1])
    except Exception:
        # даже при ошибке обязательно ACK, чтобы Telegram не "крутил"
        await callback.answer()
        return

    # 2) складываем task_id в set (мгновенно, без БД)
    s = _pending_completions.get(key)
    if s is None:
        s = set()
        _pending_completions[key] = s
    s.add(task_id)

    # 3) обновляем время последнего клика (monotonic)
    _last_click_ts[key] = asyncio.get_running_loop().time()

    # 4) мгновенно подтверждаем callback БЕЗ текста (ничего не всплывает)
    await callback.answer()

    # 5) запускаем одного воркера, если ещё не запущен
    w = _tasks_worker.get(key)
    if w is None or w.done():
        _tasks_worker[key] = asyncio.create_task(
            _tasks_click_worker(callback.bot, chat_id, msg_id, user_id)
        )

def build_pre_remind_kb(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="5 минут", callback_data=f"pre:{task_id}:5"),
                InlineKeyboardButton(text="15 минут", callback_data=f"pre:{task_id}:15"),
                InlineKeyboardButton(text="30 минут", callback_data=f"pre:{task_id}:30"),
            ],
            [
                InlineKeyboardButton(text="45 минут", callback_data=f"pre:{task_id}:45"),
                InlineKeyboardButton(text="60 минут", callback_data=f"pre:{task_id}:60"),
                InlineKeyboardButton(text="90 минут", callback_data=f"pre:{task_id}:90"),
            ],
            [
                InlineKeyboardButton(text="Своё время (часы)", callback_data=f"precustom:{task_id}")
            ]
        ]
    )

def build_tasks_inline_kb(tasks_today: List[Dict[str, Any]]) -> InlineKeyboardMarkup:
    """
    Инлайн-клавиатура под картинкой задач.

    Кнопки создаём только для НЕВЫПОЛНЕННЫХ задач,
    но номера на кнопках соответствуют номерам в таблице (1, 2, 3...).
    """
    keyboard: list[list[InlineKeyboardButton]] = []
    row: list[InlineKeyboardButton] = []

    for idx, t in enumerate(tasks_today, start=1):
        if t.get("completed"):
            # выполненным задачам кнопок не даём
            continue

        btn = InlineKeyboardButton(
            text=str(idx),              # номер строки в таблице
            callback_data=f"task_{t['id']}"
        )
        row.append(btn)

        if len(row) >= 8:
            keyboard.append(row)
            row = []

    if row:
        keyboard.append(row)

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

@dp.message(F.text == "📋 Мои задачи")
async def show_tasks_as_image(message: types.Message):
    user_id = message.from_user.id
    user_tz = await get_user_timezone(user_id)
    tasks_today = await get_todo_tasks_for_today(user_id, user_tz)

    if not tasks_today:
        await message.answer("На сегодня у тебя нет задач ✅")
        return

    try:
        # Генерируем ОДНУ картинку по всем задачам
        file_path = await generate_tasks_table_image(tasks_today)
    except Exception as e:
        logging.exception(f"Ошибка при генерации изображения задач: {e}")
        await message.answer("Не удалось отрисовать таблицу задач 😔")
        return

    try:
        photo = FSInputFile(file_path)

        # Строим инлайн-клавиатуру с номерами задач
        kb = build_tasks_inline_kb(tasks_today)

        caption = (
            "Твои задачи на сегодня 📋\n\n"
            "Номера на кнопках соответствуют задачам в таблице сверху вниз.\n"
            "Нажми номер, чтобы отметить задачу выполненной ✅"
        )

        await message.answer_photo(
            photo=photo,
            caption=caption,
            reply_markup=kb
        )
    finally:
        try:
            os.unlink(file_path)
        except Exception:
            pass



# очень простой вариант — юзер сам вводит строку, типа "Europe/Moscow"

@dp.message(Command("timezone"))
async def timezone_handler(message: types.Message):
    parts = message.text.split(maxsplit=1)
    if len(parts) == 1:
        await message.answer(
            "Укажи свою таймзону в формате IANA, например:\n"
            "<code>/timezone Europe/Moscow</code>\n"
            "<code>/timezone America/New_York</code>"
        )
        return

    tz = parts[1].strip()
    # Можно попытаться создать ZoneInfo, чтобы проверить, что она валидная
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(tz)
    except Exception:
        await message.answer("Неизвестная таймзона. Попробуй, например: Europe/Moscow")
        return

    await set_user_timezone(message.from_user.id, tz)
    await message.answer(f"Таймзона установлена: <b>{tz}</b>")


# -------------------
# Callback для "Задать вопрос ИИ"
# -------------------
@dp.callback_query(F.data == 'ask_ai')
async def ask_ai_callback(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state in [QueryStates.processing_voice, QueryStates.processing_query]:
        await callback.answer("Подождите, идет обработка другого запроса...", show_alert=True)
        return

    # Получаем транскрипт из state
    data = await state.get_data()
    transcription = data.get('transcription')

    # Если транскрипция потерялась — пытаемся восстановить из текста сообщения (резервный вариант)
    if not transcription:
        # Попробуем прочитать последние 5 сообщений в чате и найти ту, что содержит "Расшифровка"
        try:
            async for msg in callback.message.chat.get_history(limit=5):
                if msg.text and ("Распознано:" in msg.text or "Расшифровка:" in msg.text or msg.text.startswith("🗣️ Расшифровка")):
                    # убираем префиксы
                    transcription = msg.text.replace("Распознано:", "").replace("Расшифровка:", "").replace("🗣️ Расшифровка:", "").strip()
                    break
        except Exception:
            pass

    if not transcription:
        await callback.answer("Нет транскрипции для обработки.", show_alert=True)
        return

    # Снимаем кнопку у сообщения транскрипта
    try:
        last_id = data.get('last_transcription_msg_id')
        if last_id:
            await callback.message.bot.edit_message_reply_markup(chat_id=callback.message.chat.id, message_id=last_id, reply_markup=None)
    except Exception:
        pass

    # Сбрасываем временные данные транскрипции (чтобы не мешало)
    await state.update_data(last_transcription_msg_id=None, transcription=None)

    can_make_request, used_requests, limit = await check_request_limit(callback.from_user.id)
    if not can_make_request:
        await callback.answer(f"Вы исчерпали лимит запросов на сегодня.\nИспользовано: {used_requests}/{limit}", show_alert=True)
        return

    await save_conversation(callback.from_user.id, "user", transcription)
    await state.set_state(QueryStates.processing_query)
    generating_msg = await safe_send(callback, "Формулирую ответ...", reply_markup=main_menu)

    try:
        tasks_ctx = await get_tasks_context(callback.from_user.id, limit=30)
        tasks_block = format_tasks_context(tasks_ctx)

        prompt_base = (
            f"{PROMPT_FOR_AI}\n\n"
            "TASK CONTEXT (user todo list; may be relevant):\n"
            f"{tasks_block}\n\n"
            "CONVERSATION HISTORY:\n\n"
        )
        history_limit = 99
        while history_limit > 0:
            history = await get_conversation_history(callback.from_user.id, history_limit)
            dialog_part = ""
            for msg in history:
                role = "User" if msg['role'] == 'user' else "Assistant"
                dialog_part += f"{role}: {msg['content']}\n\n"

            full_prompt = prompt_base + dialog_part + f"CURRENT USER QUESTION: {transcription}\n\n"

            if len(full_prompt) <= MAX_PROMPT_LENGTH:
                break
            history_limit -= 1

        if history_limit == 0:
            full_prompt = prompt_base + f"CURRENT USER QUESTION: {transcription}\n\n"

        ai_response = await send_to_ai(full_prompt)
        await save_conversation(callback.from_user.id, "assistant", ai_response)
        await increment_request_count(callback.from_user.id)

        await safe_delete(generating_msg)
        # Отправляем ответ
        await send_long_message(callback.message, ai_response, reply_markup=main_menu)

    except Exception as e:
        logging.exception(f"Ошибка в ask_ai_callback: {str(e)}")
        await safe_delete(generating_msg)
        await safe_send(callback.message, "Произошла ошибка при обработке запроса. Попробуйте еще раз.", reply_markup=main_menu)
    finally:
        await state.clear()
    await callback.answer()

class PreRemindStates(StatesGroup):
    waiting_custom_time = State()

@dp.callback_query(F.data.startswith("pre:"))
async def pre_remind_offset_callback(cb: types.CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id
    try:
        _, task_id_str, minutes_str = cb.data.split(":")
        task_id = int(task_id_str)
        offset_min = int(minutes_str)
    except Exception:
        await cb.answer("Ошибка формата кнопки", show_alert=True)
        return

    # Достаём remind_at_ts из БД
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT remind_at_ts FROM todo_tasks WHERE id = ? AND user_id = ?",
            (task_id, user_id),
        )
        row = await cur.fetchone()
        await cur.close()

    if not row or not row["remind_at_ts"]:
        await cb.answer("Не нашёл время события 😕", show_alert=True)
        return

    remind_at_ts = int(row["remind_at_ts"])
    notify_at_ts = remind_at_ts - offset_min * 60

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if notify_at_ts <= now_ts:
        await cb.answer("Уже слишком поздно предупреждать за столько 😅", show_alert=True)
        return

    user_tz = await get_user_timezone(user_id)
    try:
        tz = ZoneInfo(user_tz or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    notify_local = datetime.fromtimestamp(notify_at_ts, tz=timezone.utc).astimezone(tz).strftime("%d.%m %H:%M")

    await set_task_notify_time(
        task_id=task_id,
        user_id=user_id,
        notify_at_ts=notify_at_ts,
        notify_at_local=notify_local,
        notify_offset_min=offset_min
    )
    # ✅ Удаляем кнопки у сообщения "Предупредить за"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # ✅ Пишем отдельное подтверждение
    await cb.message.answer("✅ Задание зачислено")

    await cb.answer(f"Ок! Предупрежу за {offset_min} минут ✅", show_alert=False)


@dp.callback_query(F.data.startswith("precustom:"))
async def pre_remind_custom_start(cb: types.CallbackQuery, state: FSMContext):
    user_id = cb.from_user.id

    # 1) Парсим task_id из callback_data: "precustom:<task_id>"
    try:
        _, task_id_str = cb.data.split(":")
        task_id = int(task_id_str)
    except Exception:
        await cb.answer("Ошибка формата кнопки", show_alert=True)
        return

    # 2) Удаляем инлайн-кнопки у сообщения "Предупредить за"
    try:
        await cb.message.edit_reply_markup(reply_markup=None)
    except Exception:
        pass

    # 3) Достаём время события (remind_at_ts) из БД
    remind_at_ts = await get_task_remind_at_ts(task_id, user_id)
    if remind_at_ts is None:
        await cb.answer("Не нашёл время события 😕", show_alert=True)
        return

    # 4) Переходим в состояние ожидания "сколько часов"
    await state.set_state(PreRemindStates.waiting_custom_time)
    await state.update_data(task_id=task_id, remind_at_ts=int(remind_at_ts))

    await cb.message.answer(
        "✅ Задание зачислено.\n\n"
        "Введите, <b>за сколько часов до события</b> прислать предупреждение.\n"
        "Можно целым или дробным числом:\n"
        "• <code>1</code> (за 1 час)\n"
        "• <code>2.5</code> (за 2 часа 30 минут)\n\n"
        "Пример: <code>3</code>",
        parse_mode="HTML"
    )

    await cb.answer()

@dp.message(PreRemindStates.waiting_custom_time)
async def pre_remind_custom_set(message: types.Message, state: FSMContext):
    user_id = message.from_user.id
    data = await state.get_data()
    task_id = int(data["task_id"])
    remind_at_ts = int(data["remind_at_ts"])

    raw = (message.text or "").strip().replace(",", ".")  # чтобы "2,5" тоже работало

    # 1) Парсим часы
    try:
        hours = float(raw)
    except ValueError:
        await message.answer("Не понял число 😕 Введите количество часов, например: <code>3</code> или <code>2.5</code>", parse_mode="HTML")
        return

    if hours <= 0:
        await message.answer("Число часов должно быть больше нуля ⛔")
        return

    offset_seconds = int(hours * 3600)
    notify_at_ts = remind_at_ts - offset_seconds

    now_ts = int(datetime.now(timezone.utc).timestamp())
    if notify_at_ts <= now_ts:
        await message.answer("Уже слишком поздно предупреждать за столько часов 😅 Укажи меньше.")
        return

    # 2) Сформируем красивую локальную строку для отображения (когда придёт предупреждение)
    user_tz = await get_user_timezone(user_id)
    try:
        tz = ZoneInfo(user_tz or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    notify_local_str = datetime.fromtimestamp(notify_at_ts, tz=timezone.utc).astimezone(tz).strftime("%d.%m %H:%M")

    # 3) Сохраняем
    notify_offset_min = int(round(hours * 60))
    await set_task_notify_time(
        task_id=task_id,
        user_id=user_id,
        notify_at_ts=notify_at_ts,
        notify_at_local=notify_local_str,
        notify_offset_min=notify_offset_min,
    )

    await message.answer(f"✅ Задание зачислено. Предупрежу <b>{notify_local_str}</b> (за {hours:g} ч.)", parse_mode="HTML")
    await state.clear()

# -------------------
# Обработчик всех остальных сообщений
# -------------------
@dp.message()
async def handle_other_messages(message: types.Message, state: FSMContext):
    current_state = await state.get_state()
    if not current_state:
        await safe_send(message, "Пожалуйста, используйте /start для начала.", reply_markup=main_menu)
    elif current_state == QueryStates.processing_query:
        await safe_send(message, "Подождите, идет обработка другого запроса...", reply_markup=main_menu)
    elif current_state == QueryStates.processing_voice:
        await safe_send(message, "Подождите, идет обработка голосового сообщения...", reply_markup=main_menu)
    else:
        await safe_send(message, "Пожалуйста, задайте вопрос текстом или голосом, или выберите действие из меню.", reply_markup=main_menu)


# -------------------
# Защита от нажатия кнопок во время обработки
# -------------------
@dp.callback_query(QueryStates.processing_voice)
async def handle_processing_voice_callback(callback: types.CallbackQuery):
    await callback.answer("Подождите, идет обработка голосового сообщения...", show_alert=True)


@dp.callback_query(QueryStates.processing_query)
async def handle_processing_query_callback(callback: types.CallbackQuery):
    await callback.answer("Подождите, идет обработка другого запроса...", show_alert=True)

@dp.callback_query(F.data == "clear_completed_tasks")
async def clear_completed_tasks_handler(callback: types.CallbackQuery):
    user_id = callback.from_user.id
    await clear_completed_tasks(user_id)
    await callback.answer("Выполненные задачи очищены ✅", show_alert=False)
    await safe_send(callback.message, "Все выполненные задачи удалены из списка.", reply_markup=None)

@dp.callback_query(F.data == "clear_context")
async def clear_context_callback(callback: types.CallbackQuery, state: FSMContext):
    current_state = await state.get_state()
    if current_state in [QueryStates.processing_voice, QueryStates.processing_query]:
        await callback.answer("Подождите, идет обработка другого запроса...", show_alert=True)
        return

    try:
        await delete_conversation_context(callback.from_user.id)
        await callback.answer("Контекст очищен ✅", show_alert=False)
        await safe_send(callback.message, "Контекст диалога очищен.", reply_markup=None)
    except Exception as e:
        logging.error(f"Ошибка при удалении контекста в БД: {e}")
        await callback.answer("Ошибка при очистке контекста.", show_alert=True)

