"""Чтение и валидация конфигурационного файла `conf.ini`.

Ожидаемая структура файла (все секции, кроме [API], необязательны):

```ini
[API]
ID_API=
HASH_API=
PHONE=
CLOUD_PASSWORD=

[SESSION]
NAME=anon
DEVICE_MODEL=SM-G991B
SYSTEM_VERSION=Android 14
APP_VERSION=8.8.2
LANG_CODE=en

[DELAY]
MESSAGES_INTERVAL_MIN=0.05
MESSAGES_INTERVAL_MAX=0.1
```

Дополнительные (необязательные) секции: [PARSER], [OUTPUT] — см. `DEFAULTS`.
"""

from __future__ import annotations

import configparser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from utils import get_logger

CONFIG_FILE_NAME = "conf.ini"

DEFAULT_SESSION_NAME = "anon"
DEFAULT_DEVICE_MODEL = "SM-G991B"
DEFAULT_SYSTEM_VERSION = "Android 14"
DEFAULT_APP_VERSION = "8.8.2"
DEFAULT_LANG_CODE = "en"

DEFAULT_DELAY_MIN = 0.05
DEFAULT_DELAY_MAX = 0.1

DEFAULT_OUTPUT_DIR = "output"
DEFAULT_FILENAME_MAX_LENGTH = 100
DEFAULT_CREATE_EMPTY_FILE = False

DEFAULT_PROXY_ENABLED = False
DEFAULT_PROXY_TYPE = "socks5"
DEFAULT_PROXY_ADDR = ""
DEFAULT_PROXY_PORT = 1080
DEFAULT_PROXY_RDNS = True

DEFAULT_WARMUP_PARTICIPANTS = False
DEFAULT_WARMUP_LIMIT = 10000

DEFAULT_MAX_ATTEMPTS = 3

_DEFAULTS: dict[str, dict[str, str]] = {
    "SESSION": {
        "NAME": DEFAULT_SESSION_NAME,
        "DEVICE_MODEL": DEFAULT_DEVICE_MODEL,
        "SYSTEM_VERSION": DEFAULT_SYSTEM_VERSION,
        "APP_VERSION": DEFAULT_APP_VERSION,
        "LANG_CODE": DEFAULT_LANG_CODE,
    },
    "DELAY": {
        "MESSAGES_INTERVAL_MIN": str(DEFAULT_DELAY_MIN),
        "MESSAGES_INTERVAL_MAX": str(DEFAULT_DELAY_MAX),
    },
    "PARSER": {
        "WARMUP_PARTICIPANTS": str(DEFAULT_WARMUP_PARTICIPANTS),
        "WARMUP_LIMIT": str(DEFAULT_WARMUP_LIMIT),
        "MAX_ATTEMPTS": str(DEFAULT_MAX_ATTEMPTS),
    },
    "PROXY": {
        "ENABLED": str(DEFAULT_PROXY_ENABLED),
        "TYPE": DEFAULT_PROXY_TYPE,
        "ADDR": DEFAULT_PROXY_ADDR,
        "PORT": str(DEFAULT_PROXY_PORT),
        "USERNAME": "",
        "PASSWORD": "",
        "RDNS": str(DEFAULT_PROXY_RDNS),
    },
    "OUTPUT": {
        "DIR": DEFAULT_OUTPUT_DIR,
        "FILENAME_MAX_LENGTH": str(DEFAULT_FILENAME_MAX_LENGTH),
        "CREATE_EMPTY_FILE": str(DEFAULT_CREATE_EMPTY_FILE),
    },
}

_BOOL_TRUE = {"1", "true", "yes", "on", "да", "y", "t"}


class ConfigError(Exception):
    """Ошибка конфигурации: приложение не может продолжить работу."""


@dataclass(frozen=True)
class ApiCredentials:
    """Учётные данные MTProto API (секция [API])."""

    api_id: int
    api_hash: str
    phone: str
    cloud_password: Optional[str] = None


@dataclass(frozen=True)
class SessionSettings:
    """Параметры сессии и «отпечатка» устройства (секция [SESSION])."""

    name: str = DEFAULT_SESSION_NAME
    device_model: str = DEFAULT_DEVICE_MODEL
    system_version: str = DEFAULT_SYSTEM_VERSION
    app_version: str = DEFAULT_APP_VERSION
    lang_code: str = DEFAULT_LANG_CODE

    @property
    def session_path(self) -> str:
        """Имя файла сессии без расширения (Telethon сам добавит `.session`)."""
        return self.name


@dataclass(frozen=True)
class DelaySettings:
    """Задержки между запросами (секция [DELAY])."""

    messages_interval_min: float = DEFAULT_DELAY_MIN
    messages_interval_max: float = DEFAULT_DELAY_MAX

    def as_tuple(self) -> tuple[float, float]:
        """Возвращает (min, max), гарантируя min <= max."""
        low, high = self.messages_interval_min, self.messages_interval_max
        return (low, high) if low <= high else (high, low)


@dataclass(frozen=True)
class ParserSettings:
    """Параметры парсинга (секция [PARSER], необязательная)."""

    warmup_participants: bool = DEFAULT_WARMUP_PARTICIPANTS
    warmup_limit: int = DEFAULT_WARMUP_LIMIT
    max_attempts: int = DEFAULT_MAX_ATTEMPTS


@dataclass(frozen=True)
class ProxySettings:
    """Прокси для подключения к Telegram (секция [PROXY], необязательная)."""

    enabled: bool = DEFAULT_PROXY_ENABLED
    proxy_type: str = DEFAULT_PROXY_TYPE
    addr: str = DEFAULT_PROXY_ADDR
    port: int = DEFAULT_PROXY_PORT
    username: Optional[str] = None
    password: Optional[str] = None
    rdns: bool = DEFAULT_PROXY_RDNS

    def as_tuple(self) -> Optional[tuple]:
        """Кортеж в формате Telethon или None, если прокси выключен."""
        if not self.enabled or not self.addr:
            return None
        return (self.proxy_type, self.addr, self.port, self.rdns,
                self.username, self.password)


@dataclass(frozen=True)
class OutputSettings:
    """Параметры сохранения результата (секция [OUTPUT], необязательная)."""

    directory: str = DEFAULT_OUTPUT_DIR
    filename_max_length: int = DEFAULT_FILENAME_MAX_LENGTH
    create_empty_file: bool = DEFAULT_CREATE_EMPTY_FILE


@dataclass(frozen=True)
class AppConfig:
    """Полная конфигурация приложения."""

    api: ApiCredentials
    session: SessionSettings = field(default_factory=SessionSettings)
    delay: DelaySettings = field(default_factory=DelaySettings)
    parser: ParserSettings = field(default_factory=ParserSettings)
    proxy: ProxySettings = field(default_factory=ProxySettings)
    output: OutputSettings = field(default_factory=OutputSettings)
    config_path: Optional[Path] = None


# --------------------------------------------------------------------------- #
# Вспомогательные функции чтения
# --------------------------------------------------------------------------- #
def _section_name(parser: configparser.ConfigParser, section: str) -> str:
    """Имя секции без учёта регистра (в conf.ini может быть [api] вместо [API])."""
    for existing in parser.sections():
        if existing.strip().upper() == section.upper():
            return existing
    return section


def _get(parser: configparser.ConfigParser, section: str, option: str) -> str:
    """Значение из конфига с учётом значений по умолчанию."""
    try:
        value = parser.get(_section_name(parser, section), option.upper(), raw=True)
    except (configparser.NoSectionError, configparser.NoOptionError):
        value = _DEFAULTS.get(section, {}).get(option, "")
    return value.strip()


def _get_float(parser: configparser.ConfigParser, section: str, option: str,
               default: float) -> float:
    raw = _get(parser, section, option)
    if not raw:
        return default
    try:
        return float(raw.replace(",", "."))
    except ValueError as exc:
        raise ConfigError(
            f"Параметр [{section}] {option} должен быть числом, получено: {raw!r}"
        ) from exc


def _get_int(parser: configparser.ConfigParser, section: str, option: str,
             default: int) -> int:
    raw = _get(parser, section, option)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(
            f"Параметр [{section}] {option} должен быть целым числом, получено: {raw!r}"
        ) from exc


def _get_bool(parser: configparser.ConfigParser, section: str, option: str,
              default: bool) -> bool:
    raw = _get(parser, section, option)
    if not raw:
        return default
    return raw.strip().lower() in _BOOL_TRUE


# --------------------------------------------------------------------------- #
# Загрузка конфигурации
# --------------------------------------------------------------------------- #
def find_config_file(base_dir: Path, explicit: Optional[Path] = None) -> Path:
    """Определяет путь к conf.ini.

    Порядок поиска: путь из аргумента -> каталог приложения -> текущий каталог.
    """
    candidates: list[Path] = []
    if explicit is not None:
        explicit_path = Path(explicit)
        if not explicit_path.is_file():
            raise ConfigError(
                f"Файл конфигурации не найден: {explicit_path}\n"
                "Проверьте путь, указанный в параметре --config."
            )
        candidates.append(explicit_path)
    candidates.extend([Path(base_dir) / CONFIG_FILE_NAME,
                       Path.cwd() / CONFIG_FILE_NAME])

    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()

    raise ConfigError(
        "Файл конфигурации conf.ini не найден.\n"
        f"Ожидался файл рядом с приложением: {Path(base_dir) / CONFIG_FILE_NAME}\n"
        "Скопируйте conf.example.ini в conf.ini и заполните [API] ID_API / HASH_API / PHONE "
        "(значения можно получить на https://my.telegram.org)."
    )


def load_config(base_dir: Path, config_path: Optional[Path] = None) -> AppConfig:
    """Читает и валидирует conf.ini. При ошибке выбрасывает `ConfigError`."""
    logger = get_logger("config")
    path = find_config_file(base_dir, config_path)

    parser = configparser.ConfigParser(interpolation=None)
    parser.optionxform = str.upper  # имена параметров без учёта регистра
    try:
        # Явно указываем utf-8: на Windows локаль может быть cp1251.
        with open(path, "r", encoding="utf-8-sig") as file:
            parser.read_file(file)
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Не удалось прочитать {path}: {exc}") from exc
    except configparser.Error as exc:
        raise ConfigError(f"Некорректный формат {path}: {exc}") from exc

    # ---------------- [API] ----------------
    if _section_name(parser, "API") not in parser.sections():
        raise ConfigError(f"В {path} отсутствует обязательная секция [API].")

    api_id_raw = _get(parser, "API", "ID_API")
    api_hash = _get(parser, "API", "HASH_API")
    phone = _get(parser, "API", "PHONE")
    cloud_password = _get(parser, "API", "CLOUD_PASSWORD")

    if not api_id_raw:
        raise ConfigError("Не заполнен [API] ID_API (целое число с my.telegram.org).")
    try:
        api_id = int(api_id_raw.replace(" ", ""))
    except ValueError as exc:
        raise ConfigError(f"[API] ID_API должен быть целым числом, получено: {api_id_raw!r}") from exc

    if not api_hash:
        raise ConfigError("Не заполнен [API] HASH_API (api_hash с my.telegram.org).")
    if not phone:
        raise ConfigError("Не заполнен [API] PHONE (номер в международном формате, например +79991234567).")

    # ---------------- [SESSION] ----------------
    session_name = _get(parser, "SESSION", "NAME") or DEFAULT_SESSION_NAME
    session_name = session_name.replace("\\", "_").replace("/", "_").strip() or DEFAULT_SESSION_NAME
    session = SessionSettings(
        name=session_name,
        device_model=_get(parser, "SESSION", "DEVICE_MODEL") or DEFAULT_DEVICE_MODEL,
        system_version=_get(parser, "SESSION", "SYSTEM_VERSION") or DEFAULT_SYSTEM_VERSION,
        app_version=_get(parser, "SESSION", "APP_VERSION") or DEFAULT_APP_VERSION,
        lang_code=_get(parser, "SESSION", "LANG_CODE") or DEFAULT_LANG_CODE,
    )

    # ---------------- [DELAY] ----------------
    delay_min = _get_float(parser, "DELAY", "MESSAGES_INTERVAL_MIN", DEFAULT_DELAY_MIN)
    delay_max = _get_float(parser, "DELAY", "MESSAGES_INTERVAL_MAX", DEFAULT_DELAY_MAX)
    if delay_min < 0 or delay_max < 0:
        raise ConfigError("[DELAY] значения интервалов не могут быть отрицательными.")
    if delay_min > delay_max:
        logger.warning(
            "MESSAGES_INTERVAL_MIN (%s) больше MESSAGES_INTERVAL_MAX (%s) — значения меняются местами.",
            delay_min, delay_max,
        )
        delay_min, delay_max = delay_max, delay_min
    delay = DelaySettings(messages_interval_min=delay_min, messages_interval_max=delay_max)

    # ---------------- [PARSER] ----------------
    parser_settings = ParserSettings(
        warmup_participants=_get_bool(parser, "PARSER", "WARMUP_PARTICIPANTS",
                                      DEFAULT_WARMUP_PARTICIPANTS),
        warmup_limit=max(0, _get_int(parser, "PARSER", "WARMUP_LIMIT", DEFAULT_WARMUP_LIMIT)),
        max_attempts=max(1, _get_int(parser, "PARSER", "MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS)),
    )

    # ---------------- [PROXY] ----------------
    proxy_type = (_get(parser, "PROXY", "TYPE") or DEFAULT_PROXY_TYPE).lower()
    if proxy_type not in ("socks5", "socks4", "http", "https"):
        raise ConfigError(
            f"[PROXY] TYPE должен быть socks5, socks4 или http, получено: {proxy_type!r}"
        )
    proxy = ProxySettings(
        enabled=_get_bool(parser, "PROXY", "ENABLED", DEFAULT_PROXY_ENABLED),
        proxy_type=proxy_type,
        addr=_get(parser, "PROXY", "ADDR"),
        port=_get_int(parser, "PROXY", "PORT", DEFAULT_PROXY_PORT),
        username=_get(parser, "PROXY", "USERNAME") or None,
        password=_get(parser, "PROXY", "PASSWORD") or None,
        rdns=_get_bool(parser, "PROXY", "RDNS", DEFAULT_PROXY_RDNS),
    )

    # ---------------- [OUTPUT] ----------------
    output_dir = _get(parser, "OUTPUT", "DIR")
    if not output_dir:
        output_dir = DEFAULT_OUTPUT_DIR
    output = OutputSettings(
        directory=output_dir,
        filename_max_length=max(20, min(200, _get_int(parser, "OUTPUT", "FILENAME_MAX_LENGTH",
                                                      DEFAULT_FILENAME_MAX_LENGTH))),
        create_empty_file=_get_bool(parser, "OUTPUT", "CREATE_EMPTY_FILE",
                                    DEFAULT_CREATE_EMPTY_FILE),
    )

    logger.info("Конфигурация загружена из %s (api_id=%s)", path.name, api_id)
    return AppConfig(
        api=ApiCredentials(
            api_id=api_id,
            api_hash=api_hash,
            phone=phone,
            cloud_password=cloud_password or None,
        ),
        session=session,
        delay=delay,
        parser=parser_settings,
        proxy=proxy,
        output=output,
        config_path=path,
    )


def session_file_path(base_dir: Path, config: AppConfig) -> Path:
    """Путь к файлу сессии (без учёта расширения `.session`)."""
    session_name = config.session.name or DEFAULT_SESSION_NAME
    path = Path(session_name)
    if not path.is_absolute():
        path = Path(base_dir) / session_name
    return path


def ensure_output_dir(base_dir: Path, directory: str) -> Path:
    """Создаёт (при необходимости) каталог для результатов и возвращает путь к нему."""
    path = Path(directory)
    if not path.is_absolute():
        path = Path(base_dir) / directory
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ConfigError(f"Не удалось создать каталог для результатов {path}: {exc}") from exc
    return path


__all__ = [
    "ApiCredentials", "AppConfig", "ConfigError", "DelaySettings",
    "OutputSettings", "ParserSettings", "ProxySettings", "SessionSettings",
    "CONFIG_FILE_NAME", "ensure_output_dir", "find_config_file",
    "load_config", "session_file_path",
]
