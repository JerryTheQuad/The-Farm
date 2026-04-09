# Универсальный RSS-бот (YouTube + RSS + Reddit)

Бот не привязан к Telegram/Discord и может работать полностью автономно:

- **PC-уведомления** через системные уведомления (`notify-send`/`osascript`)
- **Android + PC** через **self-hosted ntfy** (open-source)

## Почему подходит под задачу

1. Поддерживает:
   - YouTube feed (`type: youtube` + `channel_id`)
   - обычные RSS/Atom (`type: rss` + `url`)
   - Reddit (`type: reddit` + `subreddit` или `url`)
2. Нет привязки к экосистемам мессенджеров.
3. Для Reddit предусмотрены анти-бот обходы уровня RSS:
   - полноценный browser-like User-Agent,
   - опциональные cookies,
   - fallback на `old.reddit.com`.
4. Open-source стек:
   - Python + SQLite,
   - ntfy (можно поднять у себя дома/VPS).

## Быстрый старт

```bash
python Code.py run --config config.json
```

### Пример `config.json`

```json
{
  "db_path": "seen.db",
  "poll_interval_sec": 180,
  "desktop_notifications": true,
  "desktop_timeout_ms": 7000,
  "ntfy": {
    "enabled": true,
    "base_url": "https://ntfy.your-domain.ru",
    "topic": "my-rss-bot",
    "token": "OPTIONAL_BEARER_TOKEN"
  },
  "feeds": [
    {
      "name": "YouTube OpenAI",
      "type": "youtube",
      "channel_id": "UCXZCJLdBC09xxGZ6gcdrc6A"
    },
    {
      "name": "Habr RSS",
      "type": "rss",
      "url": "https://habr.com/ru/rss/all/all/?fl=ru"
    },
    {
      "name": "Reddit Python",
      "type": "reddit",
      "subreddit": "Python",
      "cookies": "OPTIONAL_COOKIE_STRING"
    }
  ]
}
```

## Режим отладки (один проход)

```bash
python Code.py run --config config.json --once
```

## Управление фидами без редактирования `config.json`

Теперь можно хранить и менять список фидов через SQLite-команды:

```bash
# Добавить обычный RSS
python Code.py add-feed --name "Habr" --feed-type rss --url "https://habr.com/ru/rss/all/all/?fl=ru"

# Добавить YouTube
python Code.py add-feed --name "OpenAI YouTube" --feed-type youtube --channel-id "UCXZCJLdBC09xxGZ6gcdrc6A"

# Добавить Reddit
python Code.py add-feed --name "Reddit Python" --feed-type reddit --subreddit Python

# Показать список
python Code.py list-feeds --all

# Выключить/включить/удалить по ID
python Code.py disable-feed --id 2
python Code.py enable-feed --id 2
python Code.py remove-feed --id 2
```

Если в базе есть фиды, бот использует их. Если база пустая — берёт фиды из `config.json`.

## Docker (для back4app container deploy)

В репозиторий добавлен `Dockerfile`.
Контейнер поднимает health endpoint (`/healthz`) на порту `$PORT` (по умолчанию `8080`),
чтобы платформы вроде Back4App считали деплой успешным.

Пример локального запуска:

```bash
docker build -t rss-bot .
docker run --rm -p 8080:8080 -e PORT=8080 -v "$(pwd)/config.json:/app/config.json" rss-bot
```

## Рекомендация для Android без сторонних сервисов

- Развернуть **свой ntfy-сервер**.
- В приложении ntfy на Android подписаться на ваш topic на вашем домене.
- Бот публикует уведомления напрямую на ваш сервер.
