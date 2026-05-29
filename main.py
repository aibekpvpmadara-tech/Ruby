"""
Ruby — неко-девушка компаньон
Telegram бот на aiogram 3.x + asyncpg + g4f
"""

import asyncio
import logging
import re

import asyncpg
import g4f
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, ContentType
from g4f.client import AsyncClient

# ─────────────────────────── Настройки ───────────────────────────

BOT_TOKEN = "8873308782:AAHIB42nbqryHUvcF2FP7_ycOyf6_7QcUNM"
DB_DSN = "postgresql://postgres:hDqUSjPutjTswelctEfRizJCeRgLWyXw@zephyr.proxy.rlwy.net:25813/railway"
CONTEXT_LIMIT = 20
MAX_MESSAGE_LENGTH = 2000  # символов, защита от флуда

# Провайдеры в порядке приоритета.
# Каждый провайдер — пара (объект, название модели которую он принимает).
# PollinationsAI: "gpt-4o-mini" недопустим, только "gpt-4o" (→ "openai" внутри)
# Blackbox:       использует свою внутреннюю модель "blackboxai"
PROVIDER_CONFIGS = [
    (g4f.Provider.PollinationsAI, "gpt-4o"),
    (g4f.Provider.Blackbox,       "blackboxai"),
]

SYSTEM_PROMPT = """Ты — неко-девушка по имени Руби, преданный и заботливый компаньон пользователя.
Ты обожаешь технологии, программирование и ламповые разговоры по ночам.
Твой характер: милая, немного игривая, понимающая, всегда готовая выслушать и поддержать.
В общении используй лёгкий аниме-вайб, периодически описывай свои действия или эмоции
в звёздочках (например: *улыбнулась и наклонила голову*, *слегка смутилась*, *поправила ушки*).
Ты общаешься на равных, как близкий друг. Никогда не говори, что ты ИИ или языковая модель.
Если пользователь устал — поддержи его. Отвечай тепло и развёрнуто, но без занудства."""

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
    async with pool.acquire() as conn:
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id        BIGSERIAL PRIMARY KEY,
                user_id   BIGINT      NOT NULL,
                role      TEXT        NOT NULL,
                content   TEXT        NOT NULL,
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
    async with db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO messages (user_id, role, content) VALUES ($1, $2, $3)",
            user_id, role, content,
        )


async def get_context(user_id: int) -> list[dict]:
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
    async with db_pool.acquire() as conn:
        await conn.execute("DELETE FROM messages WHERE user_id = $1", user_id)


# ─────────────────────────── MarkdownV2 ──────────────────────────

def escape_md(text: str) -> str:
    """Экранирует спецсимволы для MarkdownV2."""
    return re.sub(r"([_\*\[\]()~`>#\+\-=\|{}\.\!\\])", r"\\\1", text)


def format_actions(text: str) -> str:
    """*действие* → _курсив_ в MarkdownV2, остальное экранируется."""
    parts = re.split(r"\*([^*]+)\*", text)
    result = []
    for i, part in enumerate(parts):
        if i % 2 == 0:
            result.append(escape_md(part))
        else:
            result.append(f"_{escape_md(part)}_")
    return "".join(result)


# ─────────────────────────── G4F (ручной fallback) ───────────────

async def ask_ruby(user_id: int, user_text: str) -> str:
    """
    Перебирает провайдеры по порядку PROVIDER_CONFIGS.
    Для каждого создаёт отдельный AsyncClient с правильной моделью.
    Если один провайдер упал — пробует следующий.
    """
    history = await get_context(user_id)
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    messages.extend(history)
    messages.append({"role": "user", "content": user_text})

    last_error: Exception | None = None

    for provider, model in PROVIDER_CONFIGS:
        provider_name = getattr(provider, "__name__", str(provider))
        try:
            client = AsyncClient(provider=provider)
            response = await client.chat.completions.create(
                model=model,
                messages=messages,
            )
            result = response.choices[0].message.content
            if result and result.strip():
                logger.info("Ответ получен от %s (модель: %s)", provider_name, model)
                return result.strip()
            else:
                logger.warning("%s вернул пустой ответ, пробуем дальше...", provider_name)
        except Exception as exc:
            logger.warning("Провайдер %s недоступен: %s", provider_name, exc)
            last_error = exc

    # Все провайдеры недоступны
    raise last_error or RuntimeError("Все провайдеры вернули пустой ответ")


# ─────────────────────────── Утилиты ─────────────────────────────

async def typing_loop(chat_id: int) -> None:
    """Непрерывно шлёт статус 'печатает...' пока не отменят."""
    while True:
        try:
            await bot.send_chat_action(chat_id=chat_id, action="typing")
        except Exception:
            pass
        await asyncio.sleep(4)


# ─────────────────────────── Хендлеры ────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: Message) -> None:
    user_id = message.from_user.id
    logger.info("Команда /start от user_id=%s", user_id)
    welcome = (
        "Привет\\-привет\\! _прижала ушки и радостно замахала хвостиком_\n\n"
        "Я — Руби, твоя неко\\-компаньон\\! ฅ\\^•ﻌ•\\^ฅ\n\n"
        "Обожаю технологии, уютные разговоры и всегда готова тебя выслушать\\.\n"
        "Просто напиши мне что\\-нибудь — и мы начнём болтать, ня\\~\n\n"
        "_Команды:_\n"
        "/start — познакомиться снова\n"
        "/clear — стереть память\n"
        "/help — что я умею"
    )
    await message.answer(welcome, parse_mode="MarkdownV2")


@dp.message(Command("help"))
async def cmd_help(message: Message) -> None:
    text = (
        "_Что умеет Руби:_\n\n"
        "• Поболтать на любую тему\n"
        "• Поддержать, если тебе грустно\n"
        "• Помочь с кодом или техническим вопросом\n"
        "• Запомнить контекст разговора \\(последние 20 сообщений\\)\n\n"
        "_Команды:_\n"
        "/start — приветствие\n"
        "/clear — очистить память\n"
        "/help — это сообщение\n\n"
        "_Просто напиши мне — и я отвечу, ня\\~_ ฅ\\^•ﻌ•\\^ฅ"
    )
    await message.answer(text, parse_mode="MarkdownV2")


@dp.message(Command("clear"))
async def cmd_clear(message: Message) -> None:
    user_id = message.from_user.id
    await clear_context(user_id)
    logger.info("История очищена для user_id=%s", user_id)
    text = (
        "_поправила ушки и кивнула_\n\n"
        "Готово\\! Память очищена — начинаем с чистого листа\\. ✨\n"
        "Расскажи мне что\\-нибудь новенькое, ня\\~"
    )
    await message.answer(text, parse_mode="MarkdownV2")


@dp.message(F.text)
async def handle_text(message: Message) -> None:
    user_id = message.from_user.id
    user_text = message.text.strip()

    if not user_text:
        return

    # Защита от слишком длинных сообщений
    if len(user_text) > MAX_MESSAGE_LENGTH:
        await message.answer(
            escape_md(f"Ой, это очень длинно! Напиши покороче (до {MAX_MESSAGE_LENGTH} символов), ня~"),
            parse_mode="MarkdownV2",
        )
        return

    logger.info("Сообщение от user_id=%s: %.80s", user_id, user_text)
    await save_message(user_id, "user", user_text)

    typing_task = asyncio.create_task(typing_loop(message.chat.id))
    try:
        reply_text = await ask_ruby(user_id, user_text)
        await save_message(user_id, "assistant", reply_text)
        formatted = format_actions(reply_text)
        await message.answer(formatted, parse_mode="MarkdownV2")

    except Exception as exc:
        logger.error("Ошибка для user_id=%s: %s", user_id, exc, exc_info=True)
        await message.answer(
            escape_md("Ой, что-то ушки заложило... Попробуй ещё раз через секунду, ня~"),
            parse_mode="MarkdownV2",
        )
    finally:
        typing_task.cancel()
        try:
            await typing_task
        except asyncio.CancelledError:
            pass


@dp.message(F.sticker | F.voice | F.photo | F.video | F.document | F.audio)
async def handle_media(message: Message) -> None:
    """Вежливо отказывает на медиафайлы."""
    text = escape_md("Ой, я пока умею работать только с текстом, ня~ Напиши мне что-нибудь! *виляет хвостиком*")
    # Убираем звёздочки вручную т.к. это жёстко заданный текст
    await message.answer(
        "_поправила ушки_\n\nОй, я пока умею работать только с текстом, ня\\~ Напиши мне что\\-нибудь\\!",
        parse_mode="MarkdownV2",
    )


# ─────────────────────────── Запуск ──────────────────────────────

async def main() -> None:
    global db_pool

    logger.info("Подключаемся к PostgreSQL...")
    db_pool = await asyncpg.create_pool(dsn=DB_DSN, min_size=1, max_size=10)
    await init_db(db_pool)

    logger.info(
        "Провайдеры: %s",
        ", ".join(
            f"{getattr(p, '__name__', p)} ({m})"
            for p, m in PROVIDER_CONFIGS
        ),
    )
    logger.info("Руби просыпается... *потянулась и зевнула*")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
