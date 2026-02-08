import asyncio
import os
from dotenv import load_dotenv

from aiogram import Bot
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
TARGET_CHANNEL = os.getenv("REPORT_TARGET_CHANNEL", "@dnevnikrib")  # куда постить/закреплять
BOT_USERNAME = os.getenv("BOT_USERNAME", "expertfishing")

POST_TEXT = (
    "🧾 Добавить рыболовный отчёт\n\n"
    "Нажми кнопку ниже — откроется чат с ботом, и ты заполнишь отчёт по шаблону.\n"
    "Публикация в канал — анонимно."
)

async def main():
    bot = Bot(token=TELEGRAM_TOKEN)

    url = f"https://t.me/{BOT_USERNAME}?start=report"

    kb = InlineKeyboardBuilder()
    kb.button(text="➕ Добавить отчёт", url=url)
    kb.adjust(1)

    msg = await bot.send_message(
        chat_id=TARGET_CHANNEL,
        text=POST_TEXT,
        reply_markup=kb.as_markup(),
        disable_web_page_preview=True,
    )

    # ПИН (бот должен иметь право закреплять)
    await bot.pin_chat_message(chat_id=TARGET_CHANNEL, message_id=msg.message_id, disable_notification=True)

    await bot.session.close()
    print("OK: posted & pinned", msg.message_id)

if __name__ == "__main__":
    asyncio.run(main())
