#!/bin/bash
set -e  # Остановка при ошибке

echo "🚀 Установка..."
pip install -r requirements.txt

echo "📦 Установка yt-dlp..."
# Для Debian/Ubuntu
if command -v apt-get &> /dev/null; then
    apt-get update
    apt-get install -y yt-dlp
else
    pip install yt-dlp
fi

echo "✅ Версия yt-dlp:"
yt-dlp --version || echo "⚠️ Не найден"

echo "✅ Готово!"
