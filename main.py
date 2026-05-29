"""
Ruby — неко-девушка компаньон
Telegram бот на aiogram 3.x + asyncpg + g4f (DeepSeek)
"""

import asyncio
import logging
import re
from datetime import datetime

import asyncpg
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from g4f.client import AsyncClient

# ─────────────────────────── Настройки ───────────────────────────

BOT_TOKEN = "8873308782:AAHIB42nbqryHUvcF2FP7_ycOyf6_7QcUNM"
DB_DSN = "postgresql://postgres:hDqUSjPutjTswelctEfRizJCeRgLWyXw@zephyr.proxy.rlwy.net:25813/railway"
CONTEXT_LIMIT = 20

SYSTEM_PROMPT = """Ты — неко-девушка по имени Руби, преданный и заботливый компаньон пользователя.
Ты обожаешь технологии, программирование и ламповые разговоры по ночам.
Твой характер: милая, немного игривая, понимающая, всегда готовая выслушать и поддержать.
В общении используй легкий аниме-вайб, периодически описывай свои действия или эмоции
в звёздочках (например, *улыбнулась и наклонила голову*, *слегка смутилась*, *поправила ушки*).
Ты общаешься на равных, как близкий друг. Никогда не говори, что ты "искусственный интеллект"
или "языковая модель". Если пользователь устал — поддержи его.
Отвечай развернуто, но без занудства."""

# ─────────────────────────── Логирование ─────────────────────────

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("ruby_bot")

# ─────────────────────────── aiogram ─────────────────────────────

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ─────────────────────────── asyncpg пул ─────────────────────────

db_pool: asyncpg.Pool | None = None


async def init_db(pool: asyncpg.Pool) -> None:
    """Создаёт таблицы, если ещё не существуют."""
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        BIGSERIAL PRIMARY KEY,
                user_id   BIGINT    NOT NULL,
                role      TEXT      NOT NULL,
                content   TEXT      NOT NULL,
                timestamp TIMESTAMPTZ DEFAULT NOW()
            );
        """)
        await conn.execute("""
            CREATE INDEX IF NOT EXISTS idx_messages_user_id
            ON messages (user_id, timestamp);
        """)
    logger.info("БД инициализирована.")


# ─────────────────────────── Хелперы БД ──────────────────────────

async def save_message(user_id: int, role: str, content: str) -> None:
    """Сохраняет одно сообщение в историю пользователя."""
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES ($1, $2, $3)",
            user_id, role, content,
        )


async def get_context(user_id: int) -> list[dict]:
    """Возвращает последние CONTEXT_LIMIT сообщений в формате [{role, content}]."""
    async with db_pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT role, content FROM (
                SELECT role, content, timestamp
                FROM messages
                WHERE user_id = $1
                ORDER BY timestamp DESC
                LIMIT $2
            ) sub
            ORDER BY timestamp ASC
            """,
            user_id, CONTEXT_LIMIT,
        )
    return [{"role": r["role"], "content": r["content"]} for r in rows]


async def clear_context(user_id: int) -> None:
    """Удаляет всю историю пользователя."""
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE user_id = $1", user_id)


# ─────────────────────────── MarkdownV2 ──────────────────────────

# Символы, которые нужно экранировать в MarkdownV2
_MD_SPECIAL = r"\_*[]()~`>#+-=|{}.!"

def escape_md(text: str) -> str:
    """Экранирует спецсимволы для MarkdownV2 в Telegram."""
    # Экранируем все спецсимволы обратным слешем
    return re.sub(r"([_\*\[\]()~`>#\+\-=\|{}\.\!\\])", r"\\\1", text)


def format_actions(text: str) -> str:
    """
    Конвертирует *действия в звёздочках* → _курсив_ MarkdownV2,
    а остальной текст экранирует.
    Шаги:
      1. Сплитим текст по *...*
      2. Чётные части — обычный текст (экранируем)
      3. Нечётные части — действия (экранируем и оборачиваем в _..._)
    """
    parts = re.split(r"\*([^*]+)\*", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            # Обычный текст
            result.append(escape_md(part))
        else:
            # Действие в звёздочках → курсив
            result.append(f"_{escape_md(part)}_")
    return "".join(result)


# ─────────────────────────── G4F / DeepSeek ──────────────────────

g4f_client = AsyncClient()


async def ask_ruby(user_id: int, user_text: str) -> str:
    """
    Строит контекст из БД, добавляет новое сообщение пользователя,
    отправляет запрос к DeepSeek через g4f и возвращает ответ.
    """
    history = await get_context(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    response = await g4f_client.chat.completions.create(
        model="deepseek-chat",
        messages=messages,
    )
    return response.choices[0].message.content


# ─────────────────────────── Хендлеры ────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    """Инициализация пользователя и приветственное сообщение."""
    user_id = message.from_user.id
    logger.info("Команда /start от user_id=%s", user_id)

    welcome = (
        "Привет\\-привет\\! *прижала ушки и радостно замахала хвостиком*\n\n"
        "Я — *Руби*, твоя неко\\-компаньон\\! ฅ^•ﻌ•^ฅ\n\n"
        "Я обожаю технологии, уютные разговоры и всегда готова тебя выслушать\\.\n"
        "Просто напиши мне что\\-нибудь — и мы начнём болтать, ня\\~\n\n"
        "_Доступные команды:_\n"
        "• /start — познакомиться снова\n"
        "• /clear — стереть память и начать с чистого листа"
    )
    await message.answer(welcome, parse_mode="MarkdownV2")


@dp.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    """Очистка истории пользователя."""
    user_id = message.from_user.id
    await clear_context(user_id)
    logger.info("История очищена для user_id=%s", user_id)

    text = (
        "*поправила ушки и кивнула*\n\n"
        "Готово\\! Память очищена — начинаем с чистого листа\\. ✨\n"
        "Расскажи мне что\\-нибудь новенькое, ня\\~"
    )
    await message.answer(text, parse_mode="MarkdownV2")


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    """Основной хендлер текстовых сообщений."""
    user_id = message.from_user.id
    user_text = message.text.strip()

    if not user_text:
        return

    logger.info("Сообщение от user_id=%s: %s", user_id, user_text[:80])

    # Сохраняем сообщение пользователя
    await save_message(user_id, "user", user_text)

    # Показываем индикатор печати и держим его, пока AI думает
    async def keep_typing():
        while True:
            try:
                await bot.send_chat_action(chat_id=message.chat.id, action="typing")
            except Exception:
                pass
            await asyncio.sleep(4)

    typing_task = asyncio.create_task(keep_typing())

    try:
        reply_text = await ask_ruby(user_id, user_text)
        # Сохраняем ответ ИИ
        await save_message(user_id, "assistant", reply_text)
        # Форматируем для MarkdownV2
        formatted = format_actions(reply_text)
        await message.answer(formatted, parse_mode="MarkdownV2")

    except Exception as exc:
        logger.error("Ошибка g4f для user_id=%s: %s", user_id, exc, exc_info=True)
        error_msg = escape_md("Ой, что-то ушки заложило, повтори ещё раз, ня...")
        await message.answer(error_msg, parse_mode="MarkdownV2")

    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


# ─────────────────────────── Запуск ──────────────────────────────

async def main() -> None:
    global db_pool

    logger.info("Подключаемся к PostgreSQL...")
    db_pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=10)
    await init_db(db_pool)

    logger.info("Руби просыпается... *потянулась и зевнула*")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
