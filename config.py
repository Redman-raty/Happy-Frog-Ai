import os
from dotenv import load_dotenv

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "bot.db")

load_dotenv()

# Токен бота Telegram
BOT_TOKEN = os.getenv('BOT_TOKEN')

# Настройки для Polza.ai API
POLZA_AI_API_KEY = os.getenv('POLZA_AI_API_KEY')
POLZA_AI_BASE_URL = os.getenv('POLZA_AI_BASE_URL', 'https://api.polza.ai/api/v1')
AI_MODEL = os.getenv('AI_MODEL', 'deepseek/deepseek-r1')  # DeepSeek с reasoning

# Настройки для Nexara API (распознавание голоса)
NEXARA_API_KEY = os.getenv('NEXARA_API_KEY')
NEXARA_API_URL = os.getenv('NEXARA_API_URL', 'https://api.nexara.ru/api/v1/audio/transcriptions')

# Ограничение токенов в ответе
MAX_TOKENS = int(os.getenv('MAX_TOKENS', 1600))

# Путь к базе данных
DB_PATH = os.getenv('DB_PATH', 'bot.db')

# Лимит запросов на пользователя в день
MAX_REQUESTS_PER_DAY = int(os.getenv('MAX_REQUESTS_PER_DAY', 10))

MAX_MESSAGE_LENGTH = int(os.getenv('MAX_MESSAGE_LENGTH', 4096))

MAX_PROMPT_LENGTH = int(os.getenv('MAX_PROMPT_LENGTH', 20000))

# Промпт для ИИ
PROMPT_FOR_AI = os.getenv('PROMPT_FOR_AI', '')

# Настройки Reasoning
REASONING_ENABLED = os.getenv('REASONING_ENABLED', 'true').lower() == 'true'
REASONING_EFFORT = os.getenv('REASONING_EFFORT', 'medium')  # low, medium, high

#Импортируем админов
ADMIN_IDS = os.getenv('ADMIN_IDS', '').split(',')
#промт для 1 раунда
MAX_TASKS_PER_IMAGE = 18

FACTCHECK_ENABLED = True
FACTCHECK_MAX_SOURCES = 5