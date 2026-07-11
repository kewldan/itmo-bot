"""Точка входа: инициализация БД, бота и фонового поллера."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand

if TYPE_CHECKING:
    from aiogram.fsm.storage.base import BaseStorage

from app.backup import backup_loop
from app.config import settings
from app.cross import cross_loop
from app.db import Database
from app.handlers import routers
from app.poller import poll_loop

COMMANDS = [
    BotCommand(command="track", description="Следить за конкурсным списком"),
    BotCommand(command="status", description="Детальный разбор и вероятность"),
    BotCommand(command="chart", description="Графики динамики"),
    BotCommand(command="compare", description="Сравнить мои программы"),
    BotCommand(command="list", description="Мои подписки"),
    BotCommand(command="settings", description="Дайджест места: периодичность"),
    BotCommand(command="refresh", description="Обновить данные с сайта"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="cancel", description="Отменить текущее действие"),
]


class MissingTokenError(SystemExit):
    """BOT_TOKEN не задан."""

    def __init__(self) -> None:
        """Сообщение фиксировано."""
        super().__init__("BOT_TOKEN не задан — заполните .env (см. .env.example)")


def _storage() -> BaseStorage:
    if settings.redis_url:
        return RedisStorage.from_url(settings.redis_url)
    return MemoryStorage()


async def main() -> None:
    """Запускает бота и поллер до остановки процесса."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not settings.bot_token:
        raise MissingTokenError

    db = Database(settings.database_url)
    await db.connect()

    bot = Bot(
        settings.bot_token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dispatcher = Dispatcher(storage=_storage())
    dispatcher["db"] = db
    for router in routers:
        dispatcher.include_router(router)

    await bot.set_my_commands(COMMANDS)
    poller = asyncio.create_task(poll_loop(bot, db))
    backup = asyncio.create_task(backup_loop(bot, db))
    cross = asyncio.create_task(cross_loop(db))
    try:
        await dispatcher.start_polling(bot)
    finally:
        poller.cancel()
        backup.cancel()
        cross.cancel()
        await db.close()


if __name__ == "__main__":
    asyncio.run(main())
