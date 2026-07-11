"""Роутеры aiogram: диалог подписки, команды и загрузка CSV из ЛК."""

from app.handlers.commands import router as commands_router
from app.handlers.track import router as track_router
from app.handlers.upload import router as upload_router

routers = [track_router, upload_router, commands_router]
