# SMM Agent

Codex создаёт сценарий и визуалы, монтирует запись и назначает публикации непосредственно на YouTube, в Telegram и Дзене.

1. `/video-script`: тема и время → сводка → OK → DOCX.
2. Пользователь записывает raw-видео.
3. `/render-docx-visuals "<raw_mp4>" --docx "<docx_path>"`: монтаж и проверка.
4. `/publish-from-script "<rendered_mp4>" --docx "<docx_path>"`: общее превью → OK → серверная отложка.

DOCX и MP4 имеют одинаковый base-name `YYYY-MM-DD-HHMM-Русский заголовок`. Время — Москва. Явный запрос одной площадки ограничивает выпуск ею. Существующий YouTube URL не вызывает повторную загрузку.

Готовое видео: `.tmp/rendered/YYYY-MM-DD-HHMM-Русский заголовок.mp4`. Для старых сценариев без встроенных изображений доступен резервный `render-with-visuals`; основной способ — `render-docx-visuals`.

## Настройка

```text
python -m pip install -r requirements-publishing.txt
python tools/publish_release.py auth-youtube
python tools/publish_release.py auth-telegram
```

Создайте локальный `.env` по `.env.example`. YouTube: `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET` от OAuth Web application и ID канала; в Google Cloud добавьте Authorized redirect URI `http://localhost:8765/` (со слешем). Authorized JavaScript origins для этого способа не нужны. Вместо пары ID/Secret можно оставить резервный OAuth Desktop JSON. Telegram: api_id/api_hash и пользовательский вход с правом публикации. Дзен: браузерная сессия. BotFather не используется.
[Локальный вход и read-only проверка YouTube/Telegram](docs/account-access.md) описывают OAuth-туннель, MTProto-сессию и отчёты `doctor --online` без публикаций.
[Единая спецификация](docs/specs/content-release.md) содержит подробные инструкции, manifest и CLI.

Секреты — в `.env` и `.local/auth/`, вне Git. Встроенной генерации обложек API-ключ не нужен. Для монтажа нужны FFmpeg и зависимости навыка рендера.

Состояние — `.tmp/publishing/<docx-base>/`; не очищайте его до сверки результатов. CLI проверяет утверждённую версию, каналы и сроки, блокирует слепой повтор после таймаута. При ограничениях YouTube API используется Studio с проверкой существующего ролика.

Дзен — браузерный шаг агента с сохранением доказательства. Локальные тесты не подтверждают работоспособность реальных аккаунтов. Первичная внешняя приёмка — согласованный тестовый выпуск.
После подтверждения отложки компьютер для её исполнения не нужен. Проверка фактического выхода — отдельный запуск; постоянного монитора нет.

## Разработка

`python -m pytest tools -q` — активные тесты. Исторические интеграции и тесты сохранены в `archive/retired-2026-09-18/` и не входят в активный процесс.
Инструкции: `AGENTS.md`, `RECIPE.md`, `.agents/skills/`, `.agents/commands/`. Источник контракта: `docs/specs/content-release.md`.
