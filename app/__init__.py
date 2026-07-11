"""Пакет itmo-bot.

Бэкенд matplotlib фиксируется через окружение до первого импорта
библиотеки — сервер без дисплея.
"""

import os

os.environ.setdefault("MPLBACKEND", "Agg")
