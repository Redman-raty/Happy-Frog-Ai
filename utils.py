from aiogram import Bot
from playwright.async_api import async_playwright
import aiohttp
import json
import os
import io
import re
import logging
from datetime import datetime,date, timedelta
from zoneinfo import ZoneInfo
import difflib
import textwrap
import pandas as pd
import asyncio
import openpyxl
from config import DB_PATH
import aiosqlite
import html
import tempfile
from database import get_todo_tasks_for_today, complete_todo_task,get_user_timezone
from config import POLZA_AI_API_KEY, POLZA_AI_BASE_URL, AI_MODEL, MAX_TOKENS, NEXARA_API_KEY, NEXARA_API_URL, \
    REASONING_ENABLED, REASONING_EFFORT
from typing import List, Dict, Any


def clean_markdown(text: str) -> str:
    """
    Очищает текст от markdown разметки, сохраняя переносы строк между абзацами
    """
    if not text:
        return text

    # Удаляем жирный текст **text**
    text = re.sub(r'\*\*(.*?)\*\*', r'\1', text)

    # Удаляем курсив *text* или _text_
    text = re.sub(r'\*(.*?)\*', r'\1', text)
    text = re.sub(r'_(.*?)_', r'\1', text)

    # Удаляем зачеркивание ~~text~~
    text = re.sub(r'~~(.*?)~~', r'\1', text)

    # Удаляем заголовки ### Heading
    text = re.sub(r'#+\s*', '', text)

    # Удаляем блочные цитаты > text (сохраняем переносы)
    text = re.sub(r'^\s*>+\s*', '', text, flags=re.MULTILINE)

    # Удаляем инлайн-код `code`
    text = re.sub(r'`(.*?)`', r'\1', text)

    # Удаляем ссылки [text](url) - оставляем только текст
    text = re.sub(r'\[([^\]]+)\]\([^)]+\)', r'\1', text)

    # Удаляем маркдаун списков (сохраняя структуру)
    text = re.sub(r'^\s*[-*+]\s+', '• ', text, flags=re.MULTILINE)
    text = re.sub(r'^\s*\d+\.\s+', '', text, flags=re.MULTILINE)

    # Удаляем горизонтальные разделители ---
    text = re.sub(r'^\s*[-*_]{3,}\s*$', '', text, flags=re.MULTILINE)

    # Сохраняем переносы строк между абзацами - нормализуем множественные переносы
    # Заменяем 3+ переноса на 2 переноса (чтобы сохранить структуру абзацев)
    text = re.sub(r'\n{3,}', '\n\n', text)

    # Удаляем лишние пробелы в начале и конце строк, но сохраняем переносы
    lines = text.split('\n')
    cleaned_lines = []

    for line in lines:
        # Очищаем каждую строку от начальных/конечных пробелов
        cleaned_line = line.strip()
        # Если строка не пустая, добавляем ее
        if cleaned_line:
            cleaned_lines.append(cleaned_line)
        else:
            # Пустые строки сохраняем для разделения абзацев
            # Но добавляем только если предыдущая строка не была пустой
            if cleaned_lines and cleaned_lines[-1] != '':
                cleaned_lines.append('')

    # Собираем текст обратно
    text = '\n'.join(cleaned_lines)

    # Удаляем возможные пустые строки в начале и конце
    return text.strip()


async def send_to_ai(prompt: str) -> str:
    """
    Отправляет запрос к ИИ с поддержкой reasoning через прямой HTTP запрос
    """
    try:
        # Формируем данные запроса
        data = {
            "model": AI_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": prompt
                }
            ],
            "max_tokens": MAX_TOKENS,
            "stream": False
        }

        # Добавляем reasoning если включен
        if REASONING_ENABLED:
            if "deepseek" in AI_MODEL.lower():
                data["reasoning"] = {"effort": REASONING_EFFORT}
            elif "t-tech" in AI_MODEL.lower():
                data["reasoning"] = {"enabled": True}
            elif "openai/o" in AI_MODEL.lower() or "anthropic" in AI_MODEL.lower():
                data["reasoning"] = {"effort": REASONING_EFFORT}

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {POLZA_AI_API_KEY}"
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(
                    f"{POLZA_AI_BASE_URL}/chat/completions",
                    headers=headers,
                    json=data,
                    timeout=aiohttp.ClientTimeout(total=60)
            ) as response:

                if response.status not in [200, 201]:
                    error_text = await response.text()
                    logging.error(f"API Error {response.status}: {error_text}")
                    return "❌ Ошибка при обращении к сервису. Попробуйте еще раз."

                result = await response.json()

                if "choices" in result and len(result["choices"]) > 0:
                    message = result["choices"][0].get("message", {})
                    final_response = message.get("content", "❌ Пустой ответ от ИИ")
                    cleaned_response = clean_markdown(final_response)

                    if REASONING_ENABLED and "reasoning" in message:
                        logging.info("✅ Reasoning использован, ответ очищен от markdown")

                    return cleaned_response
                else:
                    logging.error(f"Неверный формат ответа от ИИ: {result}")
                    return "❌ Ошибка при обработке ответа. Попробуйте еще раз."

    except aiohttp.ClientError as e:
        logging.error(f"Ошибка подключения к ИИ: {str(e)}")
        return "❌ Ошибка подключения. Попробуйте еще раз."
    except asyncio.TimeoutError:
        logging.error("Таймаут при запросе к ИИ")
        return "❌ Таймаут при обработке запроса. Попробуйте еще раз."
    except Exception as e:
        logging.error(f"Неожиданная ошибка в send_to_ai: {str(e)}")
        return "❌ Произошла ошибка. Попробуйте еще раз."


async def transcribe_audio(audio_file_path: str) -> str:
    """
    Функция для транскрибации аудио через Nexara API
    """
    if not NEXARA_API_KEY:
        logging.error("Не настроен API-ключ для распознавания голоса")
        return "❌ Ошибка конфигурации сервиса."

    try:
        with open(audio_file_path, 'rb') as f:
            audio_data = f.read()

        form_data = aiohttp.FormData()
        form_data.add_field('file',
                            audio_data,
                            filename=os.path.basename(audio_file_path),
                            content_type='audio/ogg')
        form_data.add_field('response_format', 'text')

        headers = {"Authorization": f"Bearer {NEXARA_API_KEY}"}

        async with aiohttp.ClientSession() as session:
            async with session.post(NEXARA_API_URL, headers=headers, data=form_data) as response:
                response.raise_for_status()
                return await response.text()

    except aiohttp.ClientError as e:
        logging.error(f"Ошибка подключения к сервису распознавания голоса: {str(e)}")
        return "❌ Ошибка при распознавании голоса. Попробуйте еще раз."
    except Exception as e:
        logging.error(f"Ошибка при обработке аудио: {str(e)}")
        return "❌ Ошибка при обработке аудио. Попробуйте еще раз."

async def export_table_to_excel(table_name: str) -> str:
    async with aiosqlite.connect(DB_PATH) as conn:
        cursor = await conn.execute(f"SELECT * FROM {table_name}")
        rows = await cursor.fetchall()
        # Получаем названия столбцов
        columns = [description[0] for description in cursor.description]
        # Создаём DataFrame из данных
        df = pd.DataFrame(rows, columns=columns)
        # Сохраняем в временный файл
        with tempfile.NamedTemporaryFile(delete=False, suffix='.xlsx') as temp_file:
            df.to_excel(temp_file.name, index=False, engine='openpyxl')
        await cursor.close()
        return temp_file.name

async def get_db_file() -> str:
    return DB_PATH

async def broadcast_message(bot: Bot, text: str, photo: str = None):
    async with aiosqlite.connect(DB_PATH) as db:
        cursor = await db.execute('SELECT user_id FROM users')
        users = await cursor.fetchall()
    sent_count = 0
    for user in users:
        user_id = user[0]
        try:
            if photo:
                await bot.send_photo(user_id, photo, caption=text)
            else:
                await bot.send_message(user_id, text)
            sent_count += 1
            await asyncio.sleep(0.05)  # Задержка для безопасности
        except Exception as e:
            logging.error(f"Ошибка отправки пользователю {user_id}: {e}")
    return sent_count




def extract_json_from_ai_response(ai_response: str) -> dict | None:
    """
    Пытается вытащить JSON-объект из ответа модели.
    Убирает ```json ...```, префиксы 'json', текст вокруг и т.п.
    """
    raw = ai_response.strip()
    logging.info(f"AI raw response: {raw!r}")

    # 1) Отрежем код-блоки ```...```
    if "```" in raw:
        parts = raw.split("```")
        if len(parts) >= 3:
            raw = parts[1].strip()
        if raw.lower().startswith("json"):
            raw = raw[4:].strip()

    # 2) Префикс "json { ... }"
    if raw.lower().startswith("json"):
        brace_index = raw.find("{")
        if brace_index != -1:
            raw = raw[brace_index:]

    # 3) Взять только от первой { до последней }
    if "{" in raw and "}" in raw:
        start = raw.find("{")
        end = raw.rfind("}") + 1
        raw = raw[start:end]

    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        logging.error(f"Не удалось распарсить JSON от ИИ: {ai_response!r} -> after cleanup: {raw!r}")
        return None

TASK_SPLIT_PROMPT = """
Ты – помощник, который выделяет задачи из текста пользователя.

Верни СТРОГО JSON без пояснений.

Задача:
1) Выдели задачи, которые пользователь ХОЧЕТ СДЕЛАТЬ (todo).
2) Выдели задачи, которые пользователь УЖЕ СДЕЛАЛ (done).
3) Для каждой todo-задачи, если пользователь указал время/дату/интервал, заполни remind_at:
   - remind_at должен быть строкой в формате "YYYY-MM-DD HH:MM" в ЛОКАЛЬНОМ времени пользователя.
   - Если указано только время ("в 16:00") — используй соответствующую дату (сегодня/завтра по смыслу).
   - Если интервал ("с 14 до 16") — напоминание поставь на НАЧАЛО интервала (14:00), если не сказано иначе.
   - Если время не указано — remind_at = null.
4) Формулируй text кратко.

Формат:
{
  "todo": [{"text":"...", "remind_at":"YYYY-MM-DD HH:MM" | null}, ...],
  "done": [{"text":"..."}, ...]
}
"""

async def extract_todo_tasks_from_text(text: str, user_timezone: str) -> tuple[list[dict], list[dict]]:


    tz = ZoneInfo(user_timezone or "Europe/Moscow")
    now_local = datetime.now(tz)

    prompt = f"""{TASK_SPLIT_PROMPT}

ВАЖНО:
- Таймзона пользователя: {user_timezone}
- Сейчас у пользователя: {now_local:%Y-%m-%d %H:%M}
- "сегодня" = {now_local:%Y-%m-%d}
- "завтра" = {(now_local.date()).fromordinal(now_local.date().toordinal()+1):%Y-%m-%d}

=== ТЕКСТ ПОЛЬЗОВАТЕЛЯ ===
{text}
=== КОНЕЦ ТЕКСТА ===
"""
    ai_response = await send_to_ai(prompt)

    if ai_response.strip().startswith("❌"):
        return [], []

    data = extract_json_from_ai_response(ai_response)
    if not data:
        return [], []

    todo = []
    for item in data.get("todo", []):
        if not isinstance(item, dict):
            continue
        t = (item.get("text") or "").strip()
        ra = item.get("remind_at")
        if not t:
            continue
        todo.append({
            "text": t,
            "remind_at": ra.strip() if isinstance(ra, str) and ra.strip() else None
        })

    done = []
    for item in data.get("done", []):
        if isinstance(item, dict):
            t = (item.get("text") or "").strip()
        else:
            t = str(item).strip()
        if t:
            done.append({"text": t})

    return todo, done




async def complete_tasks_by_descriptions(user_id: int, descriptions: List[str]) -> list[str]:
    """
    По списку описаний выполненных задач ищет ближайшие невыполненные задачи на сегодня
    и помечает их выполненными.
    Возвращает список текстов задач, которые удалось пометить.
    """
    if not descriptions:
        return []

    user_tz = await get_user_timezone(user_id)
    tasks_today = await get_todo_tasks_for_today(user_id, user_tz)

    pending = [t for t in tasks_today if int(t.get("completed") or 0) == 0]
    if not pending:
        return []

    completed_texts: list[str] = []
    used_task_ids: set[int] = set()

    for desc in descriptions:
        desc = (desc or "").strip()
        if not desc:
            continue

        best_task = None
        best_score = 0.0

        for t in pending:
            if t["id"] in used_task_ids:
                continue

            task_text = t.get("text", "")
            score = difflib.SequenceMatcher(None, desc.lower(), task_text.lower()).ratio()

            if desc.lower() in task_text.lower() or task_text.lower() in desc.lower():
                score += 0.2

            if score > best_score:
                best_score = score
                best_task = t

        if best_task and best_score >= 0.4:
            ok = await complete_todo_task(best_task["id"], user_id)
            if ok:
                used_task_ids.add(best_task["id"])
                completed_texts.append(best_task["text"])

    return completed_texts

# --- HTML-шаблон для отчёта по задачам -------------------------

_TASKS_HTML_TEMPLATE = textwrap.dedent("""\
<!DOCTYPE html><html lang="ru"><head><meta charset="UTF-8">
<style>
@import url('https://fonts.googleapis.com/css2?family=Montserrat:wght@400;600;700&display=swap');
:root {
  --g: #34c759;
  --gl: #eafaea;
  --r: 14px;
}
body {
  margin: 0;
  font: 20px/1.5 "Montserrat", sans-serif;
  background: #fff;
}
.time-alert {
  color: #c62828;
  text-decoration: underline;
  font-weight: 600;
}
.remind-expired {
  color: #d32f2f;              /* насыщенный, читаемый красный */
  text-decoration: underline;
  text-decoration-thickness: 2px;
  text-underline-offset: 3px;
  font-weight: 600;
}
.report {
  width: 900px;
  margin: 0 auto;
}
.header {
  width: 90%;
  margin: 30px auto 10px;
  font-weight: 700;
  font-size: 28px;        /* БОЛЬШЕ */
}
.table-wrapper {
  width: 90%;
  margin: 0 auto 20px;
}
table {
  width: 100%;
  border-collapse: collapse;
  font-size: 22px;        /* 🔥 УВЕЛИЧЕННЫЙ ШРИФТ ДЛЯ ВСЕЙ ТАБЛИЦЫ */
}
th, td {
  border: 2px solid #000;
  padding: 14px 10px;     /* побольше отступы, чтобы смотрелось красиво */
  text-align: left;
}
thead th {
  background: var(--g);
  color: #fff;
  font-weight: 600;
  text-align: center;
  font-size: 24px;        /* крупные заголовки */
}
tbody tr.summary-row td {
  background: var(--gl);
  font-weight: 700;
  font-size: 22px;        /* итог тоже крупнее */
}
tbody td:last-child {
  text-align: center;
  font-size: 24px;        /* красивый крупный столбец статусов */
}
.task-done {
  text-decoration: line-through;
  color: #777;
}
.footer{
    width:90%;
    margin:0 auto 8px;
    text-align:right;
    font-weight:700;
    font-size: 16px;
}
</style></head><body>
<div class="report">
  <div class="header">Задачи на сегодня</div>
  <div class="table-wrapper">
    <table>
      <thead>
        <tr><th>Задача</th><th>Время</th><th>Выполнена</th></tr>
      </thead>
      <tbody>
        {{ROWS}}
      </tbody>
    </table>
  </div>
  <div class="footer">@TaskManagerFrog_bot</div>
</div></body></html>""")

# --- Пул браузера: Chromium поднимается 1 раз и reused ----------

_browser = None
_playwright = None


async def _get_browser():
    global _browser, _playwright
    if _browser is None:
        _playwright = await async_playwright().start()
        _browser = await _playwright.chromium.launch(
            headless=True,
            args=["--font-render-hinting=medium"]
        )
    return _browser


# --- async-функция, которая строит PNG в памяти -----------------

async def generate_tasks_table_image_async(
        tasks_today: List[Dict[str, Any]]
) -> io.BytesIO:
    """
    Рисует PNG с таблицей задач и возвращает BytesIO.

    В таблицу попадают задачи из списка tasks_today.
    Каждая задача нумеруется: 1., 2., 3. ...
    Выполненные задачи зачёркнуты (class="task-done") и помечены ✅.
    Время события подсвечивается (красное + подчёркнутое) ТОЛЬКО если:
      - напоминание уже отправлено (reminded = 1)
      - и задача НЕ выполнена (completed = 0)
    """
    if not tasks_today:
        raise ValueError("Нет задач для отображения")

    # Статистика
    completed_tasks = [t for t in tasks_today if int(t.get("completed") or 0) == 1]
    total = len(tasks_today)
    completed = len(completed_tasks)
    percent = int(round(completed / total * 100)) if total > 0 else 0

    # Строки таблицы
    rows = []
    for idx, t in enumerate(tasks_today, start=1):
        raw_text = str(t.get("text", ""))
        text = html.escape(raw_text)

        is_completed = int(t.get("completed") or 0) == 1
        is_reminded = int(t.get("reminded") or 0) == 1

        status_emoji = "✅" if is_completed else "❌"
        numbered_text = f"{idx}. {text}"

        if is_completed:
            text_html = f'<span class="task-done">{numbered_text}</span>'
        else:
            text_html = numbered_text

        # Время события (локальная строка)
        remind_at_local_raw = t.get("remind_at_local") or ""
        remind_at_local = html.escape(str(remind_at_local_raw))

        # Подсветка времени: только если напоминание пришло, а задача не выполнена
        if remind_at_local and is_reminded and not is_completed:
            time_html = f'<span class="time-alert">{remind_at_local}</span>'
        else:
            time_html = remind_at_local

        rows.append(f"<tr><td>{text_html}</td><td>{time_html}</td><td>{status_emoji}</td></tr>")

    # Строка итога
    summary_text = f"Итого: {completed} из {total} задач"
    summary_percent = f"{percent}% выполнено"
    rows.append(
        f'<tr class="summary-row"><td>{html.escape(summary_text)}</td>'
        f'<td></td><td>{html.escape(summary_percent)}</td></tr>'
    )

    rows_html = "\n".join(rows)

    # Собираем итоговый HTML
    html_page = _TASKS_HTML_TEMPLATE.replace("{{ROWS}}", rows_html)

    # Скриншот браузером
    browser = await _get_browser()
    page = await browser.new_page(viewport={"width": 900, "height": 1})
    await page.set_content(html_page, wait_until="networkidle")
    h = await page.evaluate("document.documentElement.scrollHeight")
    await page.set_viewport_size({"width": 900, "height": h})
    png = await page.screenshot(full_page=True, type="png")
    await page.close()

    return io.BytesIO(png)

async def generate_tasks_table_image(tasks_today: List[Dict[str, Any]]) -> str:
    """
    Рисует PNG с таблицей задач и возвращает путь к временному файлу.
    Разбиение задач на части (если их много) делается снаружи —
    сюда передаётся уже "порция" задач.
    """
    buffer = await generate_tasks_table_image_async(tasks_today)

    with tempfile.NamedTemporaryFile(delete=False, suffix=".png") as tmp:
        tmp.write(buffer.getvalue())
        file_path = tmp.name

    return file_path


def extract_simple_time_from_text(text: str, user_timezone: str) -> str | None:
    if not text:
        return None

    m = re.search(r'\b([01]?\d|2[0-3]):([0-5]\d)\b', text)
    if not m:
        return None

    hour = int(m.group(1))
    minute = int(m.group(2))

    tz = ZoneInfo(user_timezone or "Europe/Moscow")
    today = datetime.now(tz).date()
    return f"{today:%Y-%m-%d} {hour:02d}:{minute:02d}"

async def close_browser():
    global _browser, _playwright
    if _browser:
        await _browser.close()
        _browser = None
    if _playwright:
        await _playwright.stop()
        _playwright = None

_DATE_WORDS = ("вчера", "позавчера", "сегодня", "завтра")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")  # YYYY-MM-DD
DMY_RE = re.compile(r"\b\d{1,2}[./-]\d{1,2}([./-]\d{2,4})?\b")  # 13.12 / 13-12-2025

def normalize_remind_at(
    remind_at_str: str,
    user_timezone: str,
    original_text: str
) -> tuple[datetime | None, str | None, str | None]:
    """
    Возвращает (utc_dt, local_str, error_msg)

    Правило:
    - если время напоминания в прошлом (по локальной TZ пользователя) -> error_msg,
      utc_dt/local_str = None.
    - если всё ок -> utc_dt/local_str, error_msg=None.
    """
    if not remind_at_str:
        return None, None, None

    try:
        naive = datetime.strptime(remind_at_str, "%Y-%m-%d %H:%M")
    except ValueError:
        logging.error(f"[normalize_remind_at] bad format: {remind_at_str!r}")
        return None, None, "Не удалось распознать дату/время напоминания."

    try:
        tz = ZoneInfo(user_timezone or "Europe/Moscow")
    except Exception:
        tz = ZoneInfo("Europe/Moscow")

    local_dt = naive.replace(tzinfo=tz)
    now_local = datetime.now(tz)

    GRACE_SECONDS = 30
    if local_dt.timestamp() < (now_local.timestamp() - GRACE_SECONDS):
        human = local_dt.strftime("%d.%m.%Y %H:%M")
        return None, None, f"⛔ Время {human} уже прошло. Укажи будущее время."

    remind_at_utc = local_dt.astimezone(ZoneInfo("UTC"))
    remind_at_local_str = local_dt.strftime("%d.%m %H:%M")
    return remind_at_utc, remind_at_local_str, None

def format_tasks_context(tasks: list[dict]) -> str:
    if not tasks:
        return "Нет сохранённых задач."

    lines = []
    for t in tasks:
        status = "DONE" if int(t.get("completed") or 0) == 1 else "TODO"
        when = (t.get("remind_at_local") or "").strip()
        notify = (t.get("notify_at_local") or "").strip()

        extra = []
        if when:
            extra.append(f"event={when}")
        if notify:
            extra.append(f"notify={notify}")
        if extra:
            meta = " (" + ", ".join(extra) + ")"
        else:
            meta = ""

        lines.append(f"- [{status}] {t.get('text','')}{meta}")

    return "\n".join(lines)

