import os

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update, WebAppInfo
from telegram.ext import Application, CommandHandler, ContextTypes


load_dotenv()

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
MINIAPP_URL = os.getenv("MINIAPP_URL", "http://127.0.0.1:8000/")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_chat:
        return

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    text="Open Mini App",
                    web_app=WebAppInfo(url=MINIAPP_URL),
                )
            ]
        ]
    )

    await update.effective_chat.send_message(
        "Welcome! Tap the button below to open the Mini App and book an appointment.",
        reply_markup=keyboard,
    )


def main() -> None:
    if not BOT_TOKEN:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()

