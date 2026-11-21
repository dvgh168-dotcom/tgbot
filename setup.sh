#!/bin/bash
set -e

if ! command -v docker &> /dev/null; then
    echo "Устанавливаю Docker..."
    curl -fsSL https://get.docker.com | sh
fi

if [ ! -f .env ]; then
    echo "--- Первичная настройка бота ---"
    read -p "Вставь BOT_TOKEN (от BotFather): " BOT_TOKEN
    read -p "Вставь STORAGE_CHAT_ID (приватный канал с -100...): " STORAGE_CHAT_ID
    read -p "Вставь ADMIN_ID (твой Telegram ID): " ADMIN_ID

    cat <<EOF > .env
BOT_TOKEN=$BOT_TOKEN
STORAGE_CHAT_ID=$STORAGE_CHAT_ID
ADMIN_ID=$ADMIN_ID
EOF
fi

mkdir -p data

echo "Собираю и запускаю бота..."
docker compose build --no-cache
docker compose up -d

echo "✅ Готово! Бот запущен. Напиши ему /admin в Telegram для настройки рекламы."
