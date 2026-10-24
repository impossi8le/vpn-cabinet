"""Заглушки стенда не отстают от интерфейсов приложения.

**Почему отдельный тест.** Бесплатный путь падал с 500: транспорт стенда
`PrintingMail.send()` не принимал `attachments` — параметр, который появился в
протоколе `Transport`, когда письма научились вложениям. Тесты этого не видели
**принципиально**: они пользуются своим подставным транспортом, который обновили,
а стенд третий по счёту. Нашёл это живой прогон в браузере — и только когда я
дошёл до экрана выдачи, то есть через полчаса после того, как поломка появилась.

У такой поломки неприятные свойства: она не видна ни в одном тесте, не видна при
`import`, и рушит ровно ту страницу, которую человек уже прошёл до конца — он
получает конфиг и ошибку одновременно.

**Два уровня проверки, и они разной природы:**

- **поведенческий** — заглушки вызываются так, как их зовёт приложение
  (`MailSender.send_configs(...)`, методы `Payments`). Это то, что падало;
- **структурный** — подписи методов заглушек сверяются с протоколами. Поэтому
  **новый** параметр поймается даже там, где приложение его пока не передаёт:
  иначе тест был бы зелёным до первой правки вызывающего кода.

Стенд лежит вне пакета (`.claude/dev/`), поэтому загружается по пути. `uvicorn.run`
под `if __name__ == "__main__"`, так что импорт сервер не поднимает — и это тоже
проверяется: если защиту снять, тест начнёт поднимать сервер на 8111.
"""

from __future__ import annotations

import importlib.util
import inspect
import sys
from pathlib import Path

from cabinet.mail_sender import MailSender, Transport
from cabinet.payments import Payments

ROOT = Path(__file__).resolve().parent.parent
STAND_PATH = ROOT / ".claude" / "dev" / "cabinet_dev.py"


def _stand():
    """Стенд по пути: он вне пакета, обычным импортом его не достать."""
    spec = importlib.util.spec_from_file_location("cabinet_dev_stand", STAND_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["cabinet_dev_stand"] = module
    spec.loader.exec_module(module)
    return module


def _incompatibilities(fake: type, protocol: type) -> list[str]:
    """Где заглушка не может встать на место протокола.

    Правило простое и совпадает с тем, что значит «реализует протокол»: у заглушки
    обязан быть каждый параметр протокола, а своих **обязательных** параметров,
    которых протокол не знает, быть не должно — иначе вызов из приложения упадёт.

    Сверка подписей, а не `isinstance`: протоколы здесь не `runtime_checkable`, да
    и проверка одного имени метода пропустила бы ровно эту поломку — метод `send`
    был, а параметра не было.
    """
    problems: list[str] = []
    for name, method in inspect.getmembers(protocol, inspect.isfunction):
        if name.startswith("_"):
            continue
        fake_method = getattr(fake, name, None)
        if fake_method is None:
            problems.append(f"{fake.__name__}: нет метода {name}")
            continue
        expected = inspect.signature(method).parameters
        actual = inspect.signature(fake_method).parameters
        for param in expected:
            if param not in actual:
                problems.append(f"{fake.__name__}.{name}: нет параметра {param}")
        for param, spec in actual.items():
            if param == "self" or param in expected:
                continue
            if spec.default is inspect.Parameter.empty and spec.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
                inspect.Parameter.POSITIONAL_ONLY,
            ):
                problems.append(f"{fake.__name__}.{name}: лишний обязательный параметр {param}")
    return problems


def test_stand_mail_transport_can_stand_in_for_the_protocol() -> None:
    """Транспорт стенда принимает всё, что есть в протоколе `Transport`."""
    stand = _stand()

    assert _incompatibilities(stand.PrintingMail, Transport) == []


def test_stand_payments_can_stand_in_for_the_protocol() -> None:
    """Подставная ЮKassa стенда принимает всё, что есть в протоколе `Payments`."""
    stand = _stand()

    assert _incompatibilities(stand.StubPayments, Payments) == []


def test_stand_transport_accepts_the_config_letter_the_app_sends(capsys) -> None:
    """Письмо с вложением уходит через транспорт стенда — это то, что падало.

    Вызов здесь **тот же**, что делает приложение после оплаты: `send_configs` с
    вложениями. Именно он падал с `TypeError`, а страница отвечала 500 уже после
    того, как конфиг выдан.
    """
    stand = _stand()
    mail = MailSender(sender="cabinet@example.com", transport=stand.PrintingMail())

    mail.send_configs(
        "user@example.com",
        attachments=[("nl.ovpn", b"client\n", "application/x-openvpn-profile")],
        paid=False,
    )

    printed = capsys.readouterr().out
    assert "nl.ovpn" in printed, "имя вложения должно быть видно в логе стенда"
    assert "client" not in printed, "содержимое конфига в лог не печатаем"
    assert "Оплата прошла" not in printed, "за бесплатный доступ так писать нельзя"


def test_stand_payments_answers_the_way_the_app_asks() -> None:
    """Платёжный стенд отвечает на те же вызовы, что делает экран оплаты."""
    stand = _stand()
    payments = stand.StubPayments()

    payment = payments.create_payment(
        order_id="ord-1",
        amount_rub=140,
        description="подписка",
        return_url=payments.return_url("ord-1"),
    )

    assert payment.confirmation_url, "без страницы оплаты флоу не пройти"
    assert payments.get_payment(payment.id).amount_rub == 140, "сумма должна вернуться той же"


def test_stand_seeds_the_three_account_states() -> None:
    """Стенд сеет ровно те три состояния, ради которых он и нужен.

    По одному на путь экрана аккаунта: только Telegram, оба способа, только почта.
    Если состав изменится, половина ручных прогонов перестанет что-либо проверять,
    и заметить это можно было бы только по пустому экрану.
    """
    stand = _stand()
    api = stand.seeded_api()

    states = {(account.chat_id is not None, account.mail is not None) for account in api.accounts}
    assert states == {(True, False), (True, True), (False, True)}


def test_stand_does_not_start_a_server_on_import() -> None:
    """Импорт стенда сервер не поднимает: `uvicorn.run` под защитой.

    Иначе тест, который его загружает, занимал бы порт 8111 и мешал живому
    прогону — а прогон в браузере здесь единственный способ увидеть часть
    поломок.
    """
    stand = _stand()

    assert callable(stand.build)


def test_stand_confirms_the_bot_challenge_so_link_can_be_walked_live() -> None:
    """Служебный роут стенда имитирует подтверждение в боте.

    Без него флоу привязки Telegram в браузере не пройти: подтверждение делает
    бот в Telegram, а живого бота рядом нет. Роут обязан быть, и он обязан
    действительно подтверждать челлендж в подставном боте — тест зовёт его как
    кнопку и проверяет, что следующий опрос отдаёт `confirmed`.
    """
    from fastapi.testclient import TestClient

    stand = _stand()
    app = stand.build()
    # Тот самый подставной бот, которым собран стенд: роут подтверждения держит
    # его в замыкании, поэтому подменить `app.state` недостаточно.
    bot = app.state.telegram_auth  # type: ignore[attr-defined]

    addresses = {getattr(route, "path", "") for route in app.routes}
    assert "/dev/tg-confirm/{public_code}" in addresses, "нет роута подтверждения в боте"

    client = TestClient(app, raise_server_exceptions=False)
    link = bot.start_login("code-1", "hash", mode="link")
    assert link.deep_link.endswith("/dev/tg/code-1"), "ссылка стенда ведёт на страницу «бота»"

    response = client.get("/dev/tg-confirm/code-1", follow_redirects=False)

    assert response.status_code == 303
    poll = bot.poll_login("code-1", "secret", "123456")
    assert poll.status == "confirmed"
    assert poll.chat_id is not None


def test_stand_fake_bot_matches_the_real_client_shape() -> None:
    """Подставной публичный API стенда принимает те же вызовы, что живой клиент.

    Стенд подставляет `FakeTelegramAuth` вместо `TelegramAuthClient`. Если у
    одного из них параметр разойдётся (`mode`, `device_nonce`), флоу на стенде
    упадёт `TypeError` — ровно тем же классом поломки, что нашёл `PrintingMail`.
    Сверяются подписи, а не только имена: имя метода было, а параметра не было —
    и это как раз случай с `attachments`.
    """
    import inspect

    from cabinet.bot_client import TelegramAuthClient
    from tests.fakes import FakeTelegramAuth

    problems = _incompatibilities(FakeTelegramAuth, TelegramAuthClient)
    assert problems == []
    # И обратная сторона: `mode` у старта — часть формы, а не украшение.
    assert "mode" in inspect.signature(FakeTelegramAuth.start_login).parameters
