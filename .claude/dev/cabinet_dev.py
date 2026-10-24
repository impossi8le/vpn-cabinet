"""Стенд для живого прогона кабинета: `python .claude/dev/cabinet_dev.py`.

Зачем отдельный запуск, если есть `python -m cabinet`. Тот поднимает кабинет на
пустом подставном боте: аккаунтов нет, поэтому дойти до экрана привязки в браузере
нечем — а он и есть предмет проверки. Стенд сеет три аккаунта в разных состояниях
и печатает письма в консоль сервера: код подтверждения живёт в памяти процесса, и
без распечатки его некому прочитать.

Это **не** часть кабинета. Кабинет собирается из `build_app` параметрами
(зависимости — подставной бот, подставная почта и подставной публичный API бота),
и стенд не добавляет к нему ничего, кроме посева и служебных роутов. Их три:

- `/dev/as/{account_id}` — выдать сессию уже существующему аккаунту, чтобы в
  браузер можно было войти тем способом, которого иначе не пройти;
- `/dev/tg/{public_code}` — подставной «чат с ботом» с кнопкой подтверждения —
  сюда ведёт «Открыть Telegram» на экране привязки;
- `/dev/tg-confirm/{public_code}` — **имитация «человек нажал кнопку в Telegram»**:
  помечает челлендж подтверждённым в подставном боте (`tests.fakes.FakeTelegramAuth`),
  чтобы флоу привязки можно было пройти в браузере без настоящего Telegram.

В `build_app` этих роутов нет и не будет — в бою `/dev/tg-confirm` означал бы
привязку Telegram без подтверждения, то есть ровно ту ошибку, от которой сторожит
§3.3 спеки.

Запуск — конфигурацией `cabinet-dev` из `.claude/launch.json` (порт 8111) или
руками. Проверено на потоке привязки почты: экран, запрос кода, ввод, отказ по
подменённому адресу.

Стенд сеет ещё и серверы для покупки (шаги 3–4): `/buy/confirm` покажет право на
триал от подставного бота, `/buy/servers` — список и выбор серверов. Право на
триал подставной бот считает как живой: у аккаунта без Telegram — `telegram_required`,
у аккаунта с Telegram — доступно, поэтому на двух посеянных аккаунтах видны оба
состояния.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
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
from fastapi.responses import HTMLResponse, RedirectResponse  # noqa: E402

from cabinet.app import COOKIE_NAME, DEFAULT_SENDER, build_app  # noqa: E402
from cabinet.mail_sender import Attachment, MailSender  # noqa: E402
from cabinet.payments import Payment  # noqa: E402
from tests.fakes import FakeApi, FakeTelegramAuth  # noqa: E402

PORT = 8111


class StubPayments:
    """Подставная ЮKassa для стенда: платёж не создаётся у провайдера, а ведёт
    на внутреннюю страницу стенда `/dev/pay/{id}`.

    Нужна, чтобы по флоу можно было дойти до «страницы оплаты» без магазина и без
    сети. Это **не** часть кабинета: в бою клиент — `cabinet.payments.PaymentClient`.
    Сумма приходит аргументом и сохраняется как есть: стенд не считает денег, он
    только показывает, что платёж создан на ту сумму, что записал заказ.
    """

    def __init__(self) -> None:
        self.created: dict[str, dict] = {}
        self._next = 1

    def return_url(self, order_id: str) -> str:
        return f"http://127.0.0.1:{PORT}/buy/done?order_id={order_id}"

    def create_payment(
        self, *, order_id: str, amount_rub: int, description: str, return_url: str
    ) -> Payment:
        payment_id = f"pay-{self._next}"
        self._next += 1
        self.created[payment_id] = {"order_id": order_id, "amount_rub": amount_rub}
        return Payment(
            id=payment_id,
            status="pending",
            amount_rub=amount_rub,
            confirmation_url=f"/dev/pay/{payment_id}",
        )

    def get_payment(self, payment_id: str) -> Payment:
        info = self.created[payment_id]
        return Payment(id=payment_id, status="pending", amount_rub=info["amount_rub"])


class PrintingMail:
    """Транспорт, который печатает письмо в лог вместо доставки.

    Подставной транспорт из тестов держит письма в памяти и наружу их не отдаёт,
    поэтому живой прогон с ним заканчивается на «код отправлен»: ввести нечего.
    Здесь письмо уходит в stdout, а читает его тот, кто смотрит на экран.
    """

    def send(
        self,
        *,
        to: str,
        subject: str,
        body: str,
        attachments: Sequence[Attachment] = (),
    ) -> None:
        """Напечатать письмо в лог — вместе с тем, что приложено.

        `attachments` здесь не для красоты: транспорт, не принимающий вложения,
        роняет всю страницу, когда письмо с конфигом уходит после оплаты. Именно
        так и случилось — заглушка стенда отстала от интерфейса, и **бесплатный
        путь падал с 500** на письме. Живой прогон в браузере это нашёл, тесты —
        нет: тесты пользуются своим подставным транспортом, который обновили.

        Печатаем имена вложений, а не содержимое: конфиг в логе стенда не нужен,
        а видеть, что файл приложен, — нужно.
        """
        names = ", ".join(name for name, _content, _mime in attachments) or "нет"
        print(
            f"\n=== ПИСЬМО для {to} ===\nТема: {subject}\nВложения: {names}\n{body}"
            "=== конец письма ===\n",
            flush=True,
        )


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

    # Серверы для шага 4 покупки. Имена — отображаемые (их видит человек), коды —
    # идентификаторы; адресов и хостов здесь быть не может (репозиторий публичный).
    for code, name in (
        ("nl-1", "Нидерланды · Амстердам"),
        ("de-1", "Германия · Франкфурт"),
        ("fi-1", "Финляндия · Хельсинки"),
    ):
        api.add_server(code, name)
    print("серверы засеяны — /buy/servers покажет список", flush=True)
    return api


def build() -> FastAPI:
    payments = StubPayments()
    # Подставной публичный API бота: флоу привязки/входа идёт на стенде без
    # живого Telegram. Ссылку ведём на страницу стенда `/dev/tg/{code}`, где
    # «нажатие кнопки в Telegram» имитирует отдельный служебный роут.
    telegram_auth = FakeTelegramAuth(deep_link_template=f"http://127.0.0.1:{PORT}/dev/tg/{{code}}")
    app = build_app(
        store=seeded_api(),
        mail_sender=MailSender(sender=DEFAULT_SENDER, transport=PrintingMail()),
        payments=payments,
        telegram_auth=telegram_auth,
    )

    @app.get("/dev/as/{account_id}", include_in_schema=False)
    def dev_as(account_id: int) -> RedirectResponse:
        """Войти существующим аккаунтом, минуя флоу бота, — и уйти на экран."""
        token = app.state.sessions.sign_in(account_id).token  # type: ignore[attr-defined]
        response = RedirectResponse("/account", status_code=303)
        response.set_cookie(COOKIE_NAME, token, httponly=True, samesite="lax", path="/")
        return response

    @app.get("/dev/tg-confirm/{public_code}", include_in_schema=False)
    def dev_tg_confirm(public_code: str) -> RedirectResponse:
        """Служебный роут стенда: имитирует «человек нажал кнопку в Telegram».

        В бою подтверждение делает бот: он показывает человеку код и, когда тот
        жмёт «Подтвердить», помечает челлендж подтверждённым. Здесь это делается
        за него — иначе флоу привязки в браузере не пройти без настоящего
        Telegram. `chat_id` задаёт подставной бот (`FakeTelegramAuth.chat_id`);
        из адреса он не берётся, чтобы стенд не приучал к «`chat_id` из ссылки».

        В `build_app` этого роута нет и не будет: в бою он означал бы привязку
        без подтверждения — ту самую ошибку, от которой сторожит §3.3 спеки.
        """
        telegram_auth.confirm(public_code)
        return RedirectResponse("/account", status_code=303)

    @app.get("/dev/tg/{public_code}", include_in_schema=False)
    def dev_tg(public_code: str) -> HTMLResponse:
        """Подставной «чат с ботом»: страница с кнопкой подтверждения.

        Сюда ведёт «Открыть Telegram» со стенда. Кнопка ведёт на служебный роут
        `dev_tg_confirm` — как настоящая кнопка в Telegram ведёт подтверждение.
        """
        return HTMLResponse(
            "<h1>Подставной Telegram (стенд)</h1>"
            f"<p>Челлендж <span class='mono'>{public_code}</span>.</p>"
            f'<p><a href="/dev/tg-confirm/{public_code}">Подтвердить</a></p>'
            "<p>Любой непустой код подойдёт — его сверяет подставной бот.</p>"
        )

    @app.get("/dev/pay/{payment_id}", include_in_schema=False)
    def dev_pay(payment_id: str) -> HTMLResponse:
        """Подставная «страница оплаты ЮKassa».

        Куда кабинет уводит человека после создания платежа. Ничего не списывает:
        показывает номер и сумму и ведёт на экран возврата — чтобы флоу покупки
        был проходим до конца без живого магазина.
        """
        info = payments.created.get(payment_id)
        if info is None:
            return HTMLResponse("<h1>Платёж не найден</h1>", status_code=404)
        done = f"/buy/done?order_id={info['order_id']}"
        return HTMLResponse(
            "<h1>Подставная страница оплаты</h1>"
            f"<p>Платёж {payment_id}, сумма {info['amount_rub']} ₽.</p>"
            f'<p><a href="{done}">Вернуться в кабинет</a></p>'
        )

    return app


if __name__ == "__main__":
    uvicorn.run(build(), host="127.0.0.1", port=PORT)
