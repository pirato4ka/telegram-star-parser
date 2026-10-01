# Парсер звёзд (Paid Reactions) в постах Telegram-канала

Консольное приложение на Python: подключается к Telegram **пользовательским аккаунтом**
(MTProto / Telethon), проходит по *N* последним постам канала, находит посты, на которые
отправляли **звёзды**, определяет отправителей и сохраняет результат в Excel.

```
Введите наименование/id канала: @channel
(id последнего сообщения: 12345)
Введите количество последних постов для анализа: 200
Парсинг сообщений: 100%|██████████| 200/200 [00:12<00:00, 16 сообщ./s, со звёздами: 12]

Файл сохранён: C:\apps\stars_parser\output\2025-03-04_05-06-07_My Channel.xlsx
Проанализировано постов: 200
Пропущено служебных сообщений: 3
Постов со звёздами: 12
Записей в таблице: 47
```

---

## 1. Возможности

* вход по готовой `*.session` или по коду из Telegram (+ облачный пароль 2FA, до 3 попыток);
* канал задаётся как `@username`, `username`, `t.me/...`, `t.me/c/<id>`, `t.me/s/<username>`,
  числовой id или `-100...`; при ошибке — повторный запрос ввода;
* прогресс-бар, случайные задержки между запросами, автоматическая обработка `FloodWaitError`
  (ожидание `seconds + 1` и продолжение с того же места);
* устойчивость: ни одна ошибка отдельного сообщения, реактора или пересылки не роняет программу;
* «умное» определение отправителя: пользователь / канал / аноним / неизвестно;
  при отсутствии `access_hash` в кэше сессии остаётся id и `not_found`;
* опциональный прогрев кэша участников группы обсуждения (больше найденных username);
* Excel: шапка жирным, закреплённая первая строка, автоширина колонок, автофильтр,
  id — целыми числами (без экспоненциальной записи);
* безопасное имя файла: санитайзер для названий вроде `SupernovaElit Premium|Chat️`;
  при занятом файле — суффикс `_1`, `_2`, ...;
* логи в `parser.log`, краткий вывод в консоль; `conf.ini` и сессии в лог не попадают;
* Ctrl+C — корректное завершение **с сохранением уже собранных данных**.

## 2. Требования

* Python **3.10+** (проверено на 3.11), Windows 10/11 (Linux/macOS тоже работают);
* api_id / api_hash с <https://my.telegram.org> → *API development tools*.

## 3. Установка и запуск

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows
# source .venv/bin/activate       # Linux/macOS

pip install -r requirements.txt

copy conf.example.ini conf.ini    # Linux/macOS: cp conf.example.ini conf.ini
notepad conf.ini                  # заполните [API]

python main.py
```

Дальше приложение само спросит канал и количество постов.

### Необязательные аргументы

| Аргумент | Назначение |
|---|---|
| `-c, --config PATH` | путь к `conf.ini` (по умолчанию — рядом с приложением) |
| `--channel VALUE` | канал без интерактивного ввода (`@name`, ссылка, id) |
| `-n, --count N` | сколько последних постов анализировать |
| `--output-dir DIR` | куда сохранить xlsx (перекрывает `[OUTPUT] DIR`) |
| `--warmup` / `--no-warmup` | включить/выключить прогрев кэша участников |
| `--create-empty-file` | создать xlsx только с шапкой, если звёзд не найдено |
| `--log-level LEVEL` | `DEBUG`, `INFO`, `WARNING`, `ERROR` |

Пример полного неинтерактивного запуска:

```bash
python main.py --channel "https://t.me/durov" -n 500 --output-dir results
```

## 4. Конфигурация `conf.ini`

Файл лежит рядом с `main.py` (или с `stars_parser.exe`). Если его нет — приложение
выводит понятную ошибку и завершается с кодом `2`.

```ini
[API]
ID_API=1234567
HASH_API=0123456789abcdef0123456789abcdef
PHONE=+79991234567
CLOUD_PASSWORD=            ; облачный пароль (2FA); пусто — спросит в консоли

[SESSION]
NAME=anon                  ; файл сессии anon.session
DEVICE_MODEL=SM-G991B
SYSTEM_VERSION=Android 14
APP_VERSION=8.8.2
LANG_CODE=en

[DELAY]
MESSAGES_INTERVAL_MIN=0.05 ; random.uniform(MIN, MAX) между сообщениями
MESSAGES_INTERVAL_MAX=0.1
```

Дополнительные (необязательные) секции:

| Секция | Ключи |
|---|---|
| `[PROXY]` | `ENABLED`, `TYPE` (`socks5`/`socks4`/`http`), `ADDR`, `PORT`, `USERNAME`, `PASSWORD`, `RDNS` — нужен `pip install python-socks` |
| `[PARSER]` | `WARMUP_PARTICIPANTS`, `WARMUP_LIMIT`, `MAX_ATTEMPTS` (повторы ввода кода/пароля) |
| `[OUTPUT]` | `DIR` (по умолчанию `output`, `.` — рядом с приложением), `FILENAME_MAX_LENGTH`, `CREATE_EMPTY_FILE` |

Правила валидации: `ID_API` — целое число, `HASH_API` и `PHONE` — не пустые,
пустой `CLOUD_PASSWORD` = пароля нет, при `MIN > MAX` значения меняются местами
(с предупреждением в логе).

## 5. Результат: `output/YYYY-MM-DD_HH-MM-SS_<канал>.xlsx`

Лист **`Stars`**, одна строка = одна пара «пост + отправитель звёзд».

| Колонка | Описание |
|---|---|
| `message_type` | тип сообщения: `text`, `photo`, `video`, `document`, `audio`, `voice`, `poll`, `sticker`, `gif`, `webpage`, `other`; для пересланных — `video(forward)` |
| `reactor_type` | `user` / `channel` / `anonymous` / `unknown` |
| `current_channel` | канал, который парсится (название или username) |
| `current_message_id` | id поста в этом канале |
| `original_channel` | канал-источник для пересылок (для обычных постов — как `current_channel`) |
| `original_message_id` | id сообщения в канале-источнике (для обычных постов — как `current_message_id`) |
| `reactor_username` | username отправителя (без `@`); если его нет — имя и фамилия; если определить не удалось — `not_found` |
| `reactor_id` | id отправителя (пусто для анонимных) |
| `stars_count` | сколько звёзд отправил этот отправитель на этот пост |

Оформление: шапка жирным, первая строка закреплена (`freeze_panes = A2`), автофильтр,
ширина колонок по содержимому, id и количество звёзд — целыми числами (формат `0`).

## 6. Согласованные решения по открытым вопросам ТЗ

| Вопрос | Решение | Как поменять |
|---|---|---|
| 1. Точные названия колонок | Оставлены `current_*` / `original_*` из ТЗ (см. таблицу выше) | `models.COLUMNS` |
| 2. Формат `message_type` для пересылок | `video(forward)` — суффикс `(forward)` | `models.FORWARD_SUFFIX` |
| 3. `original_*` для непересланных постов | Дублируют `current_*` (канал и id поста) | в `handlers.build_records_for_message` — поставить `""`/`None` |
| 4. Дополнительные поля | Не добавлены: таблица осталась совпадать с ТЗ. При необходимости дату/ссылку/имя проще всего добавить в `models.StarRecord` + `models.COLUMNS` | `models.py` |
| 5. Файл, если звёзд не найдено | **Не создаётся**, печатается «Звёзды не найдены» | `CREATE_EMPTY_FILE=true` в `[OUTPUT]` или `--create-empty-file` |

## 7. Ограничения Telegram (важно понимать)

1. В `top_reactors` Telegram отдаёт **топ**-отправителей звёзд, а не обязательно всех.
   Полный список отправителей через MTProto, как правило, недоступен.
2. Пользователи, скрывшие себя, приходят как анонимные (`reactor_type = anonymous`) —
   определить их невозможно.
3. Чтобы получить username по id, нужен `access_hash` в кэше сессии. Если его нет — в таблице
   будет только id и `not_found`. Помогает `--warmup` (загрузка участников группы обсуждения).
4. Массовые запросы приводят к `FloodWait` — поэтому задержки из `[DELAY]` обязательны
   (при `FloodWait` приложение ждёт и продолжает автоматически).

## 8. Логи и коды завершения

`parser.log` (рядом с приложением) — ошибки, FloodWait, «не найденные» пользователи.
В консоль выводятся только предупреждения, ошибки и итог.

| Код | Значение |
|---|---|
| `0` | успех (в т. ч. «Звёзды не найдены») |
| `1` | непредвиденная ошибка |
| `2` | ошибка конфигурации (`conf.ini` нет / не заполнен / некорректен) |
| `3` | ошибка авторизации (код, 2FA, неверные api_id/api_hash) |
| `4` | нет соединения с Telegram (интернет/прокси) |
| `130` | прервано пользователем (Ctrl+C), сохранены собранные данные |

## 9. Сборка `stars_parser.exe`

```bat
build_exe.bat                     :: Windows: установит зависимости и соберёт dist\stars_parser.exe
```
или вручную:
```bash
pip install -r requirements.txt pyinstaller
pyinstaller stars_parser.spec --noconfirm --clean
```

Положите `conf.ini` **рядом с `dist\stars_parser.exe`** и запустите его: сессия
(`anon.session`), `parser.log` и папка `output` появятся в том же каталоге.
Внутри exe используется `sys.executable`, поэтому пути считаются от расположения exe.

> Статус проверок в репозитории: 67 офлайн-тестов пройдены
> (`python -m unittest discover -s tests`), синтаксис `stars_parser.spec` проверен,
> сценарии приложения проверены вплоть до сетевого вызова Telethon.
> Саму сборку exe в Linux-песочнице выполнить нельзя: в ней отсутствует
> `libpython3.11.so` (PyInstaller требует разделяемую библиотеку Python, а прав на
> `apt install libpython3.11` нет). На Windows сборка штатная — `build_exe.bat`.

## 10. Тесты

Офлайн-тесты (без Telegram и без сети):

```bash
python -m unittest discover -s tests -v
```

Покрыто: санитайзер имён файлов, валидация конфига, разбор ввода канала, определение типа
сообщения, определение реакторов (включая «не найден» и анонимных), обработка `FloodWait`,
экспорт в Excel, сценарий `main.run` (в т. ч. Ctrl+C).

## 11. Структура проекта

```
main.py          # точка входа, ввод данных, оркестрация
config.py        # чтение и валидация conf.ini
client.py        # создание и авторизация TelegramClient
handlers.py      # парсинг сообщений, определение реакторов и пересылок
models.py        # dataclass StarRecord, ParseStats, колонки
exporter.py      # Excel (pandas + openpyxl) и safe_filename
utils.py         # логгер, задержки, работа с текстом и путями
conf.example.ini # образец конфигурации (скопировать в conf.ini)
stars_parser.spec, build_exe.bat, build_exe.sh  # сборка exe через PyInstaller
tests/           # офлайн-тесты
```

## 12. Безопасность

`conf.ini`, `*.session`, `parser.log` и `output/` добавлены в `.gitignore` —
**не коммитите** свои api_id/api_hash и файлы сессий. Значения конфигурации и сессий
в логи и в консоль не выводятся (номер телефона маскируется).
