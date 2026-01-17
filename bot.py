import asyncio
import logging
import sys
import datetime
import requests
import os
import re
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from aiohttp import web 
from dotenv import load_dotenv

# --- AIOGRAM 3.x IMPORTS (ИСПРАВЛЕНО) ---
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import (
    Message, 
    ChatJoinRequest, 
    ChatMemberUpdated, 
    InlineKeyboardMarkup, 
    InlineKeyboardButton, 
    CallbackQuery
)
from aiogram.enums import ChatMemberStatus as Status
from aiogram.utils.keyboard import InlineKeyboardBuilder
from openai import OpenAI

# --- КОНФИГУРАЦИЯ ---
load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
OPENWEATHER_API_KEY = os.getenv("OPENWEATHER_API_KEY")
ZAJABRI_CHANNEL = "@zajabri"  # Канал для проверки

if not TELEGRAM_TOKEN or not OPENAI_API_KEY:
    sys.exit("❌ ОШИБКА: Не найдены ключи в .env")

# Инициализация
dp = Dispatcher()
client = OpenAI(api_key=OPENAI_API_KEY)

# Память (в реальном продакшене лучше Redis, но для старта ок)
user_histories: Dict[int, List[Dict]] = {}
subscribers: set[int] = set()

# --- ВЕБ-СЕРВЕР (Для Render) ---
async def start_web_server():
    app = web.Application()
    app.router.add_get('/', lambda r: web.Response(text="🎣 Expert Fishing Bot v2.1 is Alive!"))
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 10000))
    site = web.TCPSite(runner, '0.0.0.0', port)
    await site.start()
    print(f"🌐 Web server started on port {port}")

# --- 1. УМНЫЙ ПАРСЕР ЗАПРОСОВ ---
class SmartParser:
    @staticmethod
    def parse_fishing_query(text: str) -> Tuple[Optional[str], Optional[int], str]:
        """Вытаскивает локацию и день из текста"""
        text_lower = text.lower()
        
        # Список популярных локаций (можно расширять)
        locations = {
            'москва', 'пахра', 'истринское', 'руза', 'можайка', 'десна', 'яуза', 
            'пехорка', 'рожайка', 'северка', 'нерская', 'ока', 'москва-река', 'нмр',
            'волга', 'дубна', 'клязьма', 'химки', 'сенеж', 'озерна'
        }
        
        location = None
        day_offset = 0 # По дефолту сегодня
        day_map = {
            'сегодня': 0, 
            'завтра': 1, 
            'послезавтра': 2,
            'после': 2 # ловит "после завтра"
        }
        
        words = text_lower.split()
        
        # Ищем совпадения
        for word in words:
            clean = re.sub(r'[^\w]', '', word)
            if clean in locations:
                location = clean.capitalize()
            if clean in day_map:
                day_offset = day_map[clean]
        
        # Эвристика: если локация не найдена, ищем слово с большой буквы или длинное слово
        # (но пропускаем ключевые слова запроса)
        if not location:
            ignore = {'клев', 'прогноз', 'погода', 'рыбалка', 'будет', 'скажи', 'как', 'где'}
            for word in words:
                clean = re.sub(r'[^\w]', '', word)
                if len(clean) > 3 and clean not in ignore and clean not in day_map:
                    # Пробуем считать это локацией
                    location = clean.capitalize()
                    break
        
        return location, day_offset, text

# --- 2. ПРОВЕРКА ПОДПИСКИ ---
async def check_subscription(bot: Bot, user_id: int, chat_id: int = None) -> bool:
    try:
        member = await bot.get_chat_member(ZAJABRI_CHANNEL, user_id)
        is_subscribed = member.status in [
            Status.MEMBER, 
            Status.ADMINISTRATOR, 
            Status.CREATOR
        ]
        
        if is_subscribed:
            subscribers.add(user_id)
            return True
        else:
            # Если это чат, кикаем (опционально, сейчас просто предупредим)
            if chat_id:
                try:
                    pass # await bot.ban_chat_member(chat_id, user_id) # Раскомментируй для бана
                except:
                    pass
            return False
    except Exception as e:
        logging.warning(f"Ошибка проверки подписки: {e}")
        return True # Если ошибка API, лучше пустить, чем блокировать зря

# --- 3. ПОГОДА И ЛУНА ---
def get_moon_phase():
    phases = ["🌑 Новолуние", "🌒 Растущая", "🌓 Первая четверть", "🌔 Растущая", 
              "🌕 Полнолуние", "🌖 Убывающая", "🌗 Последняя четверть", "🌘 Старая"]
    lunar_cycle = 29.53
    date = datetime.date.today()
    known_new_moon = datetime.date(2000, 1, 6)
    days_since = (date - known_new_moon).days
    pos = days_since % lunar_cycle
    index = int((pos / lunar_cycle) * 8) % 8
    return phases[index]

def get_weather_forecast(city: str, day_offset: int) -> str | None:
    if not OPENWEATHER_API_KEY: return None
    if day_offset > 2: return "LIMIT" # API дает прогноз только на 5 дней, мы берем 3 для точности
    
    try:
        url = f"http://api.openweathermap.org/data/2.5/forecast?q={city}&appid={OPENWEATHER_API_KEY}&units=metric&lang=ru"
        r = requests.get(url, timeout=5).json()
        if str(r.get("cod")) != "200": return None

        target_date = datetime.date.today() + datetime.timedelta(days=day_offset)
        target_str = target_date.strftime("%Y-%m-%d")
        
        # Ищем прогноз на середину дня (12:00 или 15:00)
        forecasts = r.get("list", [])
        day_data = [f for f in forecasts if target_str in f["dt_txt"]]
        
        if not day_data: return None
        
        # Берем среднее значение или 12:00
        best = next((f for f in day_data if "12:00" in f["dt_txt"]), day_data[0])
        
        temp = best["main"]["temp"]
        pressure = int(best["main"]["pressure"] * 0.75006) # перевод в мм рт.ст.
        wind = best["wind"]["speed"]
        desc = best["weather"][0]["description"]
        moon = get_moon_phase()
        
        dates = ["Сегодня", "Завтра", "Послезавтра"]
        return f"📅 {dates[day_offset]} ({target_str})\n🌡 Темп: {temp}°C\n☁ Небо: {desc}\n🔽 Давление: {pressure} мм рт.ст.\n💨 Ветер: {wind} м/с\n🌙 Луна: {moon}"
    except Exception as e:
        logging.error(f"Weather error: {e}")
        return None

# --- 4. GPT ЭКСПЕРТ ---
SYSTEM_PROMPT = """
Ты — ЭЛИТНЫЙ РЫБОЛОВНЫЙ ГИД по Подмосковью с 20-летним стажем. 
Твоя задача — давать глубокий, профессиональный и структурированный анализ клева.

🛑 СТРОГИЕ ПРАВИЛА:
1. НИКОГДА не желай "удачи". Только: "Ни хвоста, ни чешуи!" или "НХНЧ!".
2. Используй рыболовный сленг умеренно (микруха, палка, плетня, течка, обратка).
3. Если даны [ДАННЫЕ ПОГОДЫ], прогноз строится СТРОГО на них (давление, ветер, фаза луны).

ФОРМАТ ОТВЕТА (Строго соблюдай структуру):

📍 **[Локация] | [Дата]**

🎣 **ГЛАВНАЯ ЦЕЛЬ:**
> Рыба (Шанс ?/10). Почему сейчас активна.

⚙️ **СНАСТИ И ПРИМАНКИ:**
• Удилище: (тест, строй)
• Оснастка: (толщина шнура, тип монтажа)
• Топ приманок: (цвета, размеры, веса)

🎯 **ТАКТИКА ПОИСКА:**
Где искать (ямки, бровки, трава). Тип проводки (ступенька, волочение, твич).

🌙 **ФАКТОРЫ (Погода/Луна):**
Как текущее давление и луна повлияют на клев.

🛠️ **ПЛАН Б:**
Кого ловить, если целевая рыба молчит.

---
Ни хвоста, ни чешуи! 🎣
"""

async def get_chat_response(user_id: int, text: str, weather: str = "", image_url: str = None) -> str:
    # Инициализация истории
    if user_id not in user_histories:
        user_histories[user_id] = [{"role": "system", "content": SYSTEM_PROMPT}]

    # Формируем промпт
    user_prompt = f"Запрос рыбака: {text}\n"
    
    if weather == "LIMIT":
        return "🔮 Брат, мой хрустальный шар работает только на 3 дня (сегодня, завтра, послезавтра). Дальше погода врет! Спроси ближе к дате. НХНЧ!"
    elif weather:
        user_prompt += f"\n📊 [ПОГОДНЫЕ ДАННЫЕ ОТ СТАНЦИИ]:\n{weather}\n\n-> Дай детальный прогноз на основе этих данных!"
    
    content_payload = [{"type": "text", "text": user_prompt}]
    
    # Если есть картинка
    if image_url:
        content_payload.append({"type": "image_url", "image_url": {"url": image_url}})
        user_prompt += " (К сообщению приложено фото)"

    # Добавляем в историю
    user_histories[user_id].append({"role": "user", "content": content_payload})
    
    # Ротация истории (экономим токены)
    if len(user_histories[user_id]) > 12:
        user_histories[user_id] = [user_histories[user_id][0]] + user_histories[user_id][-10:]

    try:
        response = client.chat.completions.create(
            model="gpt-4o-mini", # Баланс цены и качества
            messages=user_histories[user_id],
            temperature=0.6, # Меньше галлюцинаций, больше фактов
            max_tokens=900
        )
        answer = response.choices[0].message.content
        user_histories[user_id].append({"role": "assistant", "content": answer})
        return answer
    except Exception as e:
        logging.error(f"OpenAI Error: {e}")
        return "⚠️ Эхолот потерял связь... Повтори заброс! (Ошибка AI)"

# --- 5. ОБРАБОТЧИКИ (HANDLERS) ---

@dp.message(CommandStart())
async def cmd_start(message: Message):
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Проверить подписку", callback_data="check_subscription")
    await message.answer(
        "👋 **Здарова, рыбак!**\n\n"
        "Я — твой персональный AI-гид по водоемам.\n"
        "Чтобы я мог давать тебе точные прогнозы, тебе нужно быть подписанным на наш основной канал: @zajabri\n\n"
        "После подписки нажми кнопку ниже или просто напиши свой вопрос, начиная со звездочки `*`.\n\n"
        "Пример: `*Клев на Пахре завтра?`",
        reply_markup=kb.as_markup()
    )

@dp.callback_query(F.data == "check_subscription")
async def cb_check_sub(callback: CallbackQuery):
    if await check_subscription(callback.bot, callback.from_user.id):
        await callback.message.edit_text("✅ **Доступ открыт!**\n\nСпрашивай про клев (начинай со `*`) или кидай фото улова!")
    else:
        await callback.answer("❌ Ты еще не подписан на @zajabri!", show_alert=True)

@dp.chat_join_request()
async def handle_join_request(request: ChatJoinRequest):
    # Авто-прием заявки, если подписан на канал
    if await check_subscription(request.bot, request.from_user.id):
        await request.bot.approve_chat_join_request(request.chat.id, request.from_user.id)

# Основной обработчик сообщений
@dp.message(F.text.startswith("*") | F.caption.startswith("*"))
async def expert_fishing_handler(message: Message):
    # 1. Извлекаем текст
    full_text = message.caption if message.photo else message.text
    if not full_text: return
    
    # 2. Проверка подписки (для групп)
    user_id = message.from_user.id
    if message.chat.type != "private":
        if not await check_subscription(message.bot, user_id, message.chat.id):
            return # Игнорим или удаляем, если не подписан

    # 3. Визуальная реакция
    await message.bot.send_chat_action(message.chat.id, "typing")
    
    # 4. Парсинг
    query_text = full_text[1:].strip() # Убираем звездочку
    location, day_offset, clean_text = SmartParser.parse_fishing_query(query_text)
    
    # 5. Получение погоды
    weather_info = ""
    if location:
        w_data = get_weather_forecast(location, day_offset)
        if w_data:
            weather_info = w_data
        elif w_data == "LIMIT":
            weather_info = "LIMIT"

    # 6. Обработка фото
    image_url = None
    if message.photo:
        file_id = message.photo[-1].file_id
        file = await message.bot.get_file(file_id)
        image_url = f"https://api.telegram.org/file/bot{TELEGRAM_TOKEN}/{file.file_path}"

    # 7. Запрос к мозгам
    response = await get_chat_response(user_id, clean_text, weather_info, image_url)
    
    # 8. Ответ (Reply)
    await message.reply(response, parse_mode="Markdown")

# --- ЗАПУСК ---
async def main():
    bot = Bot(token=TELEGRAM_TOKEN)
    print("🚀 Bot is starting...")
    
    # Сброс вебхуков (важно для переключения между dev/prod)
    await bot.delete_webhook(drop_pending_updates=True)
    
    # Запуск веб-сервера в фоне
    asyncio.create_task(start_web_server())
    
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        print("Bot stopped!")










