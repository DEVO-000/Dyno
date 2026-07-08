#!/bin/bash

echo "🚀 Устанавливаем Python пакеты..."
pip install -r requirements.txt

echo "📦 Устанавливаем yt-dlp..."
apt-get update
apt-get install -y yt-dlp

echo "✅ Готово!"
