"""Роутеры aiogram: диалог подписки и команды."""

from app.handlers.commands import router as commands_router
from app.handlers.track import router as track_router

routers = [track_router, commands_router]
