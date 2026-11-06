"""Прогресс шагов покупки: человек видит, где он и сколько осталось (P0-1).

Покупка идёт четырьмя экранами — тариф, подтверждение, серверы, оплата, — и ни
один из них до сих пор не говорил, на каком шаге человек. Подпись шага рисует
общий фрагмент `_buy_steps.html`: шаблоны отдают ему готовый номер, а разметка
его не считает.

Крайний случай — подпись обязана исчезать, а не врать: `step` вне диапазона не
рисует ничего. Иначе выдуманный номер («Шаг 5 из 4») читался бы как сбой
страницы, а не как её устройство.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet import render
from cabinet.app import COOKIE_NAME, build_app
from cabinet.mail_sender import MailSender
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 11, 6, 12, 0, tzinfo=UTC)


def _client_for(api: FakeApi, account_id: int) -> TestClient:
    """Клиент, вошедший аккаунтом. Сессия выдаётся `SessionService`, а не
    подставляется руками: она читает аккаунт через то же хранилище."""
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(COOKIE_NAME, app.state.sessions.sign_in(account_id).token)
    return client


def test_buy_screens_show_step_progress() -> None:
    """Каждый экран покупки называет свой шаг — из четырёх.

    Номер идёт по потоку, а не по адресу: шаг 1 на тарифах, 2 на подтверждении,
    3 на серверах, 4 на оплате. Прогон через настоящие роуты: подпись, вписанная
    в один шаблон, на соседних экранах не появилась бы.
    """
    api = FakeApi()
    account = api.create_account(chat_id=555_000_111)
    client = _client_for(api, account.id)

    assert "Шаг 1 из 4" in client.get("/buy?days=30&devices=1").text
    assert "Шаг 2 из 4" in client.get("/buy/confirm?days=30&devices=1").text
    assert "Шаг 3 из 4" in client.get("/buy/servers?days=30&devices=1").text
    assert "Шаг 4 из 4" in client.get("/buy/pay?days=30&devices=1").text


def test_step_mark_is_not_drawn_out_of_range() -> None:
    """Шаг вне диапазона — подписи нет: пустое место честнее выдуманного номера.

    Фрагмент получает номер готовым и сам ничего не считает. Пятый шаг в потоке
    из четырёх не существует, и подпись «Шаг 5 из 4» была бы не мелкой
    неточностью, а выдумкой. Рисует фрагмент тот же Jinja-контекст, что и
    экраны, — иначе проверялся бы не тот шаблон, который уходит человеку.
    """
    template = render._ENV.get_template("_buy_steps.html")

    assert template.render(step=0, steps_total=4).strip() == ""
    assert template.render(step=5, steps_total=4).strip() == ""
    assert template.render(step=2, steps_total=4).strip() == '<p class="group-t">Шаг 2 из 4</p>'
