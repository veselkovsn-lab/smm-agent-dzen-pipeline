# Read-only проверка YouTube и Telegram (issue #2)

`doctor --online --platform youtube|telegram` не загружает видео и не отправляет сообщения. Он проверяет сохранённый локальный вход, целевой канал и доступные права. JSON-отчёт содержит только код причины, тип ошибки и следующий шаг; секреты и OAuth URL в отчёт не попадают. `ready` подтверждает OAuth-права и совпадение YouTube-канала либо пользовательскую сессию и право `post_messages` в Telegram. Квоту и фактический результат будущей публикации подтверждает отдельный выпуск.

Для локальных BDD-проверок установить `requirements-testing.txt`; рабочему preflight достаточно `requirements-publishing.txt`.

## Локальный вход

Заполните `.env` по `.env.example`. Токены и сессии остаются в `.local/auth/` вне Git. Коды, пароли и OAuth URL не передавайте в чат или публичный issue.

На сервере из каталога проекта:

```bash
.venv/bin/python tools/publish_release.py auth-youtube
.venv/bin/python tools/publish_release.py auth-telegram
```

Команды интерактивные. Для Google OAuth Web application зарегистрируйте callback `http://localhost:8765/` **со слешем**. Если терминал находится на сервере, а браузер — на Windows, до запуска `auth-youtube` откройте в отдельном PowerShell локальный SSH forward и держите его до завершения входа:

```powershell
ssh -N -L 127.0.0.1:8765:127.0.0.1:8765 <server>
```

OAuth URL из терминала откройте в Windows-браузере и выберите нужный YouTube-канал. Переход на `localhost:8765` вернётся к процессу на сервере через туннель. Для Telegram код входа и при необходимости 2FA вводятся **в интерактивном SSH-терминале**, не в чате. Это пользовательская MTProto-сессия с правом публикации в канале; токен BotFather не подходит.

После входа запустите проверки по отдельности:

```bash
/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/python tools/doctor.py --online --platform youtube --output .tmp/account-access/youtube.json
/root/.local/bin/codex-test-guard --timeout 10m -- .venv/bin/python tools/doctor.py --online --platform telegram --output .tmp/account-access/telegram.json
```

На Windows используйте `\.venv\Scripts\python.exe` и встроенный Windows mutex runner; серверный `codex-test-guard` там не нужен. Запускайте проверки последовательно. Код завершения `0` означает `ready`, `2` — `blocked`; поле `next` говорит, какой вход или право нужно исправить. Отчёты и сессии не прикладывайте к PR. Дзен проверяется отдельно.
