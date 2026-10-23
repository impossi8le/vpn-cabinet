"""Стенд для живого прогона кабинета: `python .claude/dev/cabinet_dev.py`.

Зачем отдельный запуск, если есть `python -m cabinet`. Тот поднимает кабинет на
пустом подставном боте: аккаунтов нет, а входа по Telegram в кабинете ещё нет
(флоу бота — волна W6), поэтому дойти до экрана привязки почты в браузере
нечем — а он и есть предмет проверки. Стенд сеет три аккаунта в разных
состояниях и печатает письма в консоль сервера: код подтверждения живёт в памяти
процесса, и без распечатки его некому прочитать.

Это **не** часть кабинета. Кабинет собирается из `build_app` параметрами
(зависимости — подставной бот и подставная почта), и стенд не добавляет к нему
ничего, кроме посева и одного служебного роута `/dev/as/{account_id}`: он выдаёт
сессию уже существующему аккаунту, чтобы в браузер можно было войти тем способом,
которого в кабинете ещё нет. В `build_app` этого роута нет и не будет — в бою он
означал бы вход без подтверждения, то есть ровно ту ошибку, от которой сторожит
§3.3 спеки.

Запуск — конфигурацией `cabinet-dev` из `.claude/launch.json` (порт 8111) или
руками. Проверено на потоке привязки почты: экран, запрос кода, ввод, отказ по
подменённому адресу.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime
from pathlib import Path

# Корень репозитория в путь импорта: скрипт запускается по своему пути, и
# `sys.path[0]` — это `.claude/dev`, а не корень, откуда видны `cabinet` и
# `tests`. Ставить пакет в окружение ради стенда незачем.
_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import uvicorn  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.responses import RedirectResponse  # noqa: E402

from cabinet.app import COOKIE_NAME, DEFAULT_SENDER, build_app  # noqa: E402
from cabinet.mail_sender import MailSender  # noqa: E402
from tests.fakes import FakeApi  # noqa: E402

PORT = 8111


class PrintingMail:
    """Транспорт, который печатает письмо в лог вместо доставки.

    Подставной транспорт из тестов держит письма в памяти и наружу их не отдаёт,
    поэтому живой прогон с ним заканчивается на «код отправлен»: ввести нечего.
    Здесь письмо уходит в stdout, а читает его тот, кто смотрит на экран.
    """

    def send(self, *, to: str, subject: str, body: str) -> None:
        print(f"\n=== ПИСЬМО для {to} ===\n{body}=== конец письма ===\n", flush=True)


def seeded_api() -> FakeApi:
    """Три состояния аккаунта из §4–5 спеки — по одному на каждый путь экрана."""
    api = FakeApi()
    now = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
    telegram_only = api.create_account(chat_id=111_222_333)
    both = api.create_account(chat_id=444_555_666, mail="both@example.com", mail_verified_at=now)
    mail_only = api.create_account(mail="mail-only@example.com", mail_verified_at=now)
    for label, account in (
        ("только Telegram — сюда привязываем почту", telegram_only),
        ("оба способа — тут видно отвязку", both),
        ("только почта — привязываем Telegram", mail_only),
    ):
        print(f"аккаунт {account.id}: {label}", flush=True)
    return api


def build() -> FastAPI:
    app = build_app(
        store=seeded_api(),
        mail_sender=MailSender(sender=DEFAULT_SENDER, transport=PrintingMail()),
    )

    @app.get("/dev/as/{account_id}", include_in_schema=False)
    def dev_as(account_id: int) -> RedirectResponse:
        """Войти существующим аккаунтом, минуя флоу бота, — и уйти на экран."""
        token = app.state.sessions.sign_in(account_id).token  # type: ignore[attr-defined]
        response = RedirectResponse("/account", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/")
        return response

    return app


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=PORT)
