"""Точка входа кабинета: боевые зависимости собираются из окружения (issue #45).

Проверяется не «модуль импортируется», а два свойства запуска:

- **без обязательных переменных окружения** `python -m cabinet` не поднимается,
  а отказывается с понятным текстом и ненулевым кодом. Молчаливый откат на
  подставного бота — и есть дефект #45: выложенный кабинет входил бы любым
  адресом и показывал подписки из фикстур;
- **с переменными** собирается `BotClient` с теми же значениями, что пришли из
  окружения, — то есть кабинет действительно пойдёт к роутеру бота, а не к моку.

Значения адресов и секретов здесь выдуманные и зарезервированные (`example.com`,
loopback): репозиторий публичный, настоящих в нём быть не может. Секрет
подставлен плейсхолдером и в тексте отказа повторяться не должен — ни целиком,
ни частями.
"""

from __future__ import annotations

import pytest

from cabinet.__main__ import build_from_env, main
from cabinet.bot_client import SECRET_HEADER, BotClient, BotConfigError

# Адрес бота — loopback: настоящий роутер `/internal/*` живёт там же, наружу не
# выставлен, а конкретный порт здесь и есть тот, что назван в контракте.
BOT_URL = "http://127.0.0.1:8007"
SECRET = "cabinet-entrypoint-test-secret"

# Почта — через HTTP-API, а не SMTP: меньше переменных в тесте, а конфиг всё
# равно боевой (`MailSender.from_env`). Значения — плейсхолдеры.
MAIL_ENV = {
    "MAIL_TRANSPORT": "api",
    "MAIL_FROM": "cabinet@example.com",
    "MAIL_API_URL": "https://api.example.com/send",
    "MAIL_API_KEY": "test-api-key",
}

FULL_ENV = {"BOT_BASE_URL": BOT_URL, "CABINET_INTERNAL_SECRET": SECRET, **MAIL_ENV}


def test_missing_bot_url_refuses_to_start():
    """Без `BOT_BASE_URL` сборка отвергается.

    `BotClient` сам подставил бы плейсхолдер (`DEFAULT_BASE_URL`), поэтому
    требование держится **до** него — в точке входа: адрес бота в бою не
    угадывается, а задаётся, и неверная настройка не должна выглядеть рабочей.
    """
    with pytest.raises(BotConfigError) as exc:
        build_from_env({**FULL_ENV, "BOT_BASE_URL": "  "})

    # В отказе назван только **имя** переменной, а не значение секрета.
    assert "BOT_BASE_URL" in str(exc.value)
    assert SECRET not in str(exc.value)


def test_missing_secret_refuses_to_start():
    """Пустой секрет — неверная настройка, а не «работаем без него».

    Роутер бота без секрета отвечает 401, и причину надо называть на старте, а
    не выводить её из чужого ответа на первом входе человека.
    """
    with pytest.raises(BotConfigError) as exc:
        build_from_env({**FULL_ENV, "CABINET_INTERNAL_SECRET": ""})

    assert "CABINET_INTERNAL_SECRET" in str(exc.value)


def test_builds_bot_client_from_environment():
    """С переменными хранилищем становится живой `BotClient`, а не подставной бот."""
    app = build_from_env(FULL_ENV)
    store = app.state.store

    assert isinstance(store, BotClient)
    assert store.base_url == BOT_URL
    # Секрет доходит до того самого заголовка, которым кабинет представляется
    # боту: проверяется контракт (`SECRET_HEADER`), а не поле объекта — иначе
    # тест закрепил бы внутреннее устройство клиента, а не его поведение.
    assert store._headers()[SECRET_HEADER] == SECRET


def test_process_refuses_and_exits_nonzero_without_environment(monkeypatch, capsys):
    """Запуск процесса без окружения: понятный текст и ненулевой код выхода.

    Переменные убираются из процесса (`os.environ`), а не подменяются словарём:
    проверяется именно то, что увидит юнит выкладки, запустив `python -m cabinet`
    без настроек. До `uvicorn.run` дело не доходит — сервер не поднимается, о чём
    и говорит код выхода.
    """
    for name in ("BOT_BASE_URL", "CABINET_INTERNAL_SECRET"):
        monkeypatch.delenv(name, raising=False)

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "BOT_BASE_URL" in err
    assert SECRET not in err
