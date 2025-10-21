import os
from dotenv import load_dotenv

# Завантажуємо змінні середовища з файлу .env
load_dotenv()

API_KEY = os.getenv('API_KEY')
API_SECRET = os.getenv('API_SECRET')
TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
CHAT_ID = os.getenv('CHAT_ID')

# Перевірка, чи всі змінні завантажено
if not all([TELEGRAM_TOKEN, CHAT_ID]):
    raise ValueError("Не вдалося завантажити TELEGRAM_TOKEN або CHAT_ID. Перевірте ваш .env файл.")