"""Служебные уведомления администратору бота."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramAPIError
from aiogram.types import BufferedInputFile

from app.config import settings

if TYPE_CHECKING:
    from aiogram import Bot

log = logging.getLogger(__name__)


async def notify_admin(bot: Bot, text: str) -> None:
    """Шлёт сообщение админу, если ADMIN_TG_ID настроен."""
    if settings.admin_tg_id is None:
        return
    try:
        await bot.send_message(settings.admin_tg_id, text)
    except TelegramAPIError:
        log.warning("Не удалось отправить сообщение админу %s", settings.admin_tg_id)


async def send_admin_document(
    bot: Bot, data: bytes, filename: str, caption: str
) -> bool:
    """Шлёт админу файл; False, если не настроено или не удалось."""
    if settings.admin_tg_id is None:
        return False
    try:
        await bot.send_document(
            settings.admin_tg_id,
            BufferedInputFile(data, filename=filename),
            caption=caption,
        )
    except TelegramAPIError:
        log.warning("Не удалось отправить файл админу %s", settings.admin_tg_id)
        return False
    return True
