"""Недоступность бота: честный экран вместо 500 (issue #49).

Зачем отдельный файл. `store.find_by_id` звался роутами **без защиты**, а
`BotUnreachable` — не разновидность `BotError`, и мимо `except BotError` проходил
дальше. Соседние вызовы того же хранилища защищены (`subscriptions_for`,
`unlink_preview`), и текст для человека на этот случай уже написан
(`app.SUBSCRIPTIONS_ERROR`), — то есть это недосмотр, а не замысел.

Почему этого не видел ни один тест. Подставной бот (`tests.fakes.FakeApi`) не
падает никогда: он и есть подстановка, у которой отказ по определению невозможен.
Поэтому здесь роль бота играет подставной **HTTP**, которого можно «выключить», а
проверяется **реальный роут** поверх настоящего `BotStore`: дефект жил на стыке
живого пути и HTTP-слоя.

Что именно проверяется, кроме кода ответа:

- **сессия не гасится.** «Бот недоступен» и «аккаунта нет» (`404
  account_not_found`) — разные состояния. Первое значит, что кабинет на время
  ослеп; выкидывать за это человека из кабинета нельзя, а второе обязано гасить
  сессию и уводить на вход. Оба случая здесь стоят рядом — иначе тест закрывал бы
  только половину различения, ради которого он написан.
- **файл не отдаётся.** Недоступность бота на скачивании — не отказ по логике, и
  `.ovpn` из неё появиться не может.

Подставной HTTP повторяет внутренний роутер бота (§1 контракта): те же пути, та
же форма ответа, тот же заголовок с секретом. Соединение подменяется транспортом
`httpx`, сеть не открывается. `tests/fakes.py` **не трогается**: подставной бот
остаётся общим для всех волн, а стык проверяется здесь.

Ничего опознаваемого в адресах нет и быть не может: репозиторий публичный, и
единственный допустимый адрес бота здесь — loopback.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import httpx
from fastapi.testclient import TestClient

from cabinet.accounts import Account
from cabinet.app import (
    BOT_UNREACHABLE_ERROR,
    COOKIE_NAME,
    LOGIN_BOT_UNREACHABLE,
    SUBSCRIPTIONS_ERROR,
    build_app,
)
from cabinet.bot_client import SECRET_HEADER, BotClient, BotStore
from cabinet.mail_sender import MailSender
from cabinet.render import render_subscriptions
from tests.fakes import STATUS_ACTIVE, FakeApi, FakeMailSender, FakeSubscription

# Адрес бота — loopback: настоящий роутер `/internal/*` живёт там же, наружу не
# выставлен. Порт значения не имеет: соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
SECRET = "bot-outage-test-secret"
NOW = datetime(2026, 10, 23, 12, 0, tzinfo=UTC)
MAIL = "user@example.com"
CHAT_ID = 123_456_789
SUBSCRIPTION_NAME = "Нидерланды · Амстердам"

# Карточка честной пустоты (W4.3). Ищем именно её заголовок: он и есть
# утверждение «список пуст», которое при отказе было бы неправдой.
EMPTY_LIST_CARD = "Подписок нет"

# Подставное содержимое профиля. Настоящего `.ovpn` здесь не будет никогда:
# адрес сервера и ключ — это то, чего в публичном репозитории быть не должно.
FAKE_BODY = b"# fake .ovpn: podstavnoe soderzhimoe\n"

# Из письма код достаётся так же, как его достал бы человек: цифры из текста.
_CODE_RE = re.compile(r"(\d{6})")


def _error(code: str, status: int) -> httpx.Response:
    """Отказ в формате контракта бота: `{"error": {"code": ...}}` (§1)."""
    return httpx.Response(status, json={"error": {"code": code, "retryable": False}})


def _account_json(account: Account) -> dict:
    """Аккаунт в форме ответа бота — та же, что у `by-chat-id`/`by-mail` (§1)."""
    return {
        "id": account.id,
        "chat_id": account.chat_id,
        "mail": account.mail,
        "mail_verified_at": (
            account.mail_verified_at.isoformat() if account.mail_verified_at else None
        ),
    }


class FlakyBot:
    """Подставной бот, у которого можно «пропасть» — целиком или по действию.

    Это и есть предмет #49: подставной бот из `tests.fakes` не падает никогда, а
    живой падает. Отказ здесь — не ответ с кодом ошибки, а **обрыв связи**
    (`httpx.ConnectError`): именно из него `BotClient` делает `BotUnreachable`, и
    именно он раньше доезжал до человека страницей сбоя сервера.

    `offline` — то, до чего сейчас не достучаться:

    - `"all"` — бот не отвечает вовсе (сеть, таймаут);
    - `"account"` — не отвечает чтение аккаунта;
    - `"configs"` — список подписок бот отдаёт, а профиль уже нет: частичный сбой,
      единственный, при котором отказ приходит **из скачивания**, а не из списка;
    - `"unlink"` — не доходит запись отвязки при живом чтении.

    `forget` — id аккаунтов, о которых бот отвечает `404 account_not_found`. Это
    второе состояние, которое нельзя смешивать с обрывом: аккаунт исчез, и
    подставной `FakeApi` забывать аккаунты не умеет — за него это делает бот,
    который в этой паре и есть источник истины.
    """

    def __init__(self, api: FakeApi) -> None:
        self.api = api
        self.offline: set[str] = set()
        self.forget: set[int] = set()
        self.paths: list[str] = []
        self.transport = httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        # Секрет проверяется на каждом запросе: роутер бота без него отвечает
        # 401, и адаптер не должен уметь сходить «мимо».
        if request.headers.get(SECRET_HEADER) != SECRET:
            return _error("invalid_secret", 401)

        self.paths.append(request.url.path)
        parts = request.url.path.strip("/").split("/")
        if parts[:2] != ["internal", "accounts"]:
            return _error("not_found", 404)

        action = self._action(request.method, parts)
        if "all" in self.offline or action in self.offline:
            raise httpx.ConnectError("бот не отвечает", request=request)

        if request.method != "GET":
            # Записи, до которых дело дошло при живом боте, тестам не нужны:
            # проверяется отказ, а не успех (успех — предмет соседних файлов).
            return _error("not_found", 404)

        account = self._lookup(request, parts)
        if account is None or account.id in self.forget:
            return _error("account_not_found", 404)

        if not parts[2].isdigit():
            # Поиск по почте или по Telegram id (§1): ответ — аккаунт целиком.
            # Нужен связыванию: занятое место оно выясняет именно так.
            return httpx.Response(200, json={"account": _account_json(account)})

        rest = parts[3:]
        if not rest:
            return httpx.Response(200, json={"account": _account_json(account)})
        if rest == ["subscriptions"]:
            return httpx.Response(
                200,
                json={
                    "subscriptions": [
                        item.to_api()
                        for item in self.api.subscriptions
                        if item.account_id == account.id
                    ]
                },
            )
        if rest == ["balance"]:
            return httpx.Response(200, json={"credit_rub": 0})
        if rest == ["referral"]:
            return httpx.Response(
                200, json={"referral_code": "CAB-0000", "invited_count": 0, "rub_earned": 0}
            )
        if rest == ["unlink-preview"]:
            return httpx.Response(200, json={"configs_affected": 0, "remaining_method": "telegram"})
        if len(rest) == 2 and rest[0] == "configs":
            return httpx.Response(
                200, content=FAKE_BODY, headers={"content-type": "application/x-openvpn-profile"}
            )
        return _error("not_found", 404)

    def _lookup(self, request: httpx.Request, parts: list[str]) -> Account | None:
        """Найти аккаунт так же, как его нашёл бы бот: по id, почте или Telegram id.

        Три пути — три способа спросить об одном и том же (`find_by_id`,
        `find_by_mail`, `find_by_chat_id`), и связывание спрашивает всеми тремя.
        """
        if len(parts) == 3 and parts[2] == "by-mail":
            return self.api.find_by_mail(request.url.params["mail"])
        if len(parts) == 3 and parts[2] == "by-chat-id":
            return self.api.find_by_chat_id(int(request.url.params["chat_id"]))
        if len(parts) >= 3 and parts[2].isdigit():
            return self.api.find_by_id(int(parts[2]))
        return None

    @staticmethod
    def _action(method: str, parts: list[str]) -> str:
        """Имя действия бота по пути — то, что гасят в `offline`."""
        if method != "GET":
            return "unlink" if parts[-1].startswith("unlink-") else "write"
        rest = parts[3:]
        if not rest:
            return "account"
        if rest == ["subscriptions"]:
            return "subscriptions"
        if rest[:1] == ["configs"]:
            return "configs"
        return "other"


def _store_over(bot: FlakyBot) -> BotStore:
    """Хранилище боевого вида: живой `BotClient` в синхронной форме `BotStore`.

    Оба клиента идут через один подставной транспорт — так же, как в бою они идут
    через один адрес: адаптер не подменяет клиента, а пользуется им.
    """
    return BotStore(
        BotClient(
            base_url=BASE,
            secret=SECRET,
            client=httpx.AsyncClient(base_url=BASE, transport=bot.transport),
        ),
        http_client=httpx.Client(base_url=BASE, transport=bot.transport),
    )


def _app_over(bot: FlakyBot, sender: FakeMailSender | None = None):
    """Приложение поверх живой формы хранилища — как на выкладке."""
    return build_app(
        store=_store_over(bot),  # type: ignore[arg-type]
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender or FakeMailSender()),
        now=lambda: NOW,
    )


def _entered_over(
    bot: FlakyBot, account_id: int, sender: FakeMailSender | None = None
) -> TestClient:
    """Клиент приложения, вошедший аккаунтом.

    Сессия выдаётся **при живом боте**: дальше тесты его гасят, а вход уже
    состоялся. Подставлять cookie вручную нельзя — подставленный cookie не
    отличить от поддельного, и «сессия пережила сбой» проверялось бы на сессии,
    которой в хранилище нет.

    `sender` нужен проверкам с почтой: код тест достаёт из подставного ящика, как
    его достал бы человек из письма, — а не из внутренностей `AuthMail`.
    """
    app = _app_over(bot, sender)
    client = TestClient(app, raise_server_exceptions=False)
    client.cookies.set(
        COOKIE_NAME,
        app.state.sessions.sign_in(account_id).token,
        domain="testserver.local",
    )
    client.app_state = app.state  # type: ignore[attr-defined]
    return client


def _subscribed_account() -> tuple[FakeApi, Account, FakeSubscription]:
    """Аккаунт с одной активной подпиской и её конфигом — то, чем живёт кабинет."""
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)
    config = api.add_config(account.id, body=FAKE_BODY.decode())
    subscription = api.add_subscription(
        account.id,
        name=SUBSCRIPTION_NAME,
        status=STATUS_ACTIVE,
        expires_at=NOW + timedelta(days=10),
        config_id=config.id,
    )
    return api, account, subscription


# --- 1. недоступность бота: экран и живая сессия ----------------------------


def test_subscriptions_screen_survives_a_bot_outage() -> None:
    """Бот не отвечает — экран с текстом отказа, а не 500, и сессия цела (#49).

    Утверждений четыре, и все обязательны. Код ответа отделяет этот дефект от
    страницы сбоя сервера; текст говорит человеку, что делать; сохранность сессии
    отделяет недоступность бота от «аккаунта нет»; а отсутствие «Подписок нет» —
    отделяет **неизвестный** список от **известного пустого**: пустой список,
    переданный в разметку, нарисовал бы рядом с отказом ещё и карточку
    «Подписок нет», и человек прочёл бы «у вас ничего нет» вместо «повторите
    позже» (см. `test_refusal_screen_does_not_claim_there_are_no_subscriptions`).
    """
    api, account, _ = _subscribed_account()
    bot = FlakyBot(api)
    client = _entered_over(bot, account.id)
    token = client.cookies.get(COOKIE_NAME)

    bot.offline.add("all")
    response = client.get("/subscriptions", follow_redirects=False)

    assert response.status_code == 200
    assert SUBSCRIPTIONS_ERROR in response.text
    assert EMPTY_LIST_CARD not in response.text
    # Сессию отказной экран не гасит: cookie приходит тем же значением и с
    # живым сроком (обновление при заходе — #53, а не выход). Гасят её только
    # на «аккаунта нет».
    cookie_header = response.headers.get("set-cookie", "")
    assert f"{COOKIE_NAME}=" in cookie_header
    assert "max-age=0" not in cookie_header.lower()
    # Сессия жива — и в хранилище сессий, и на следующем запросе.
    assert client.app_state.sessions.resolve(token) is not None
    bot.offline.clear()
    again = client.get("/subscriptions", follow_redirects=False)
    assert again.status_code == 200
    assert SUBSCRIPTION_NAME in again.text


def test_missing_account_still_signs_out() -> None:
    """`404 account_not_found` — **не** недоступность: сессия гаснет, человек на входе.

    Это вторая половина различения, без которой первая не значит ничего: сведи
    оба состояния в одно — и «аккаунт исчез» показывался бы как «обновите позже»,
    чему человек верил бы бесконечно.
    """
    api, account, _ = _subscribed_account()
    bot = FlakyBot(api)
    client = _entered_over(bot, account.id)
    token = client.cookies.get(COOKIE_NAME)

    bot.forget.add(account.id)  # бот: «такого аккаунта нет» — 404, а не обрыв
    response = client.get("/subscriptions", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"
    assert client.app_state.sessions.resolve(token) is None
    # Тот же cookie больше не пускает в кабинет: выход состоялся, а не нарисован.
    assert client.get("/subscriptions", follow_redirects=False).headers["location"] == "/login"


def test_login_methods_screen_survives_a_bot_outage() -> None:
    """Экран способов входа при недоступном боте: отказ, а не 500 и не выход.

    Состояние аккаунта кабинету сейчас неизвестно, поэтому экран **не**
    утверждает ни «привязана», ни «не привязана»: догадка о состоянии хуже
    молчания — по ней человек пошёл бы привязывать то, что уже привязано.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)
    bot = FlakyBot(api)
    client = _entered_over(bot, account.id)
    token = client.cookies.get(COOKIE_NAME)

    bot.offline.add("all")
    response = client.get("/account", follow_redirects=False)

    assert response.status_code == 200
    assert BOT_UNREACHABLE_ERROR in response.text
    assert client.app_state.sessions.resolve(token) is not None
    # Ложного состояния на экране нет: ни «привязана», ни «не привязана» —
    # состояние аккаунта кабинету сейчас неизвестно.
    assert "Не привязана" not in response.text
    assert "Не привязан" not in response.text
    # И списка подписок тоже нет: пустой список здесь означает «неизвестен», а
    # не «пуст». Рядом с отказом «Подписок нет» было бы утверждением о том,
    # чего мы не знаем.
    assert EMPTY_LIST_CARD not in response.text


# --- 2. недоступность бота на действиях ------------------------------------


def test_config_download_survives_a_partial_bot_outage() -> None:
    """Список бот отдал, профиль — нет: отказ на `200`, файла нет.

    Частичный сбой здесь не выдумка: список и профиль — два запроса, и между ними
    бот может перестать отвечать. Роут скачивания ловил только свои отказы
    (`ConfigError`), поэтому обрыв из `configs.download` доезжал до человека 500 —
    при том, что соседний отказ (истёкшая подписка) показывается экраном.
    """
    api, account, subscription = _subscribed_account()
    bot = FlakyBot(api)
    client = _entered_over(bot, account.id)
    bot.offline.add("configs")

    response = client.get(f"/subscriptions/{subscription.id}/config", follow_redirects=False)

    assert response.status_code == 200
    assert BOT_UNREACHABLE_ERROR in response.text
    # Ни файла, ни намёка на сохранение: содержимое не отдаётся при недоступности.
    assert response.content != FAKE_BODY
    assert response.headers["content-type"].startswith("text/html")
    assert "attachment" not in response.headers.get("content-disposition", "")


def test_unlink_survives_a_bot_that_stops_answering_the_write() -> None:
    """Чтение дошло, запись — нет: отказ на `200`, сессия цела.

    Отвязка читает аккаунт, а потом пишет. Сбой ровно между этими двумя
    запросами — единственное окно, в котором роут отвязки зовёт хранилище при
    недоступном боте; закрыть его обязательно, иначе редкая гонка даёт страницу
    сбоя там, где у соседних действий стоит отказ.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID, mail=MAIL, mail_verified_at=NOW)
    bot = FlakyBot(api)
    client = _entered_over(bot, account.id)
    token = client.cookies.get(COOKIE_NAME)

    bot.offline.add("unlink")
    response = client.post("/account/unlink/telegram", follow_redirects=False)

    assert response.status_code == 200
    assert BOT_UNREACHABLE_ERROR in response.text
    assert EMPTY_LIST_CARD not in response.text
    assert client.app_state.sessions.resolve(token) is not None


def test_link_mail_confirm_survives_a_bot_outage() -> None:
    """Шаг кода привязки почты при обрыве: отказ, ничего не привязано, тупика нет.

    Роут подтверждения — второе место, где обрыв доходит до хранилища:
    `SignInService.link_mail` переводит только отказы `AuthMail`, а
    `BotUnreachable` из связывания проходит насквозь. Чтение аккаунта бот отдаёт,
    `link-mail` — уже нет, и это то самое окно, в котором шаг кода остаётся
    позади, а результата нет.

    Про код здесь нельзя обещать одно и то же: `Linking.link_mail` читает аккаунт,
    а `AuthMail.verify` вызывается **до** записи (`cabinet/linking.py:243`) и код
    гасит. Поэтому проверяется не «код цел», а «человек не заперт в шаге с
    погашенным кодом»: на экран возвращается форма адреса, и новое письмо
    запрашивается обычным действием. Обещание «наберите прежний код» было бы
    дефектом — тихим: он вылез бы у человека как «код не подошёл» за сбой сети.
    """
    api = FakeApi()
    account = api.create_account(chat_id=CHAT_ID)  # только Telegram: почту есть куда привязать
    bot = FlakyBot(api)
    sender = FakeMailSender()
    client = _entered_over(bot, account.id, sender)

    # Код запрашивается при живом боте: шаг привязки его помнит и показывает.
    step = client.post("/account/link/mail", data={"mail": MAIL}, follow_redirects=False)
    assert "Код отправлен на" in step.text
    code = _CODE_RE.search(sender.sent_to(MAIL)[-1]["body"]).group(1)

    bot.offline.add("write")  # запись бота не доходит
    response = client.post(
        "/account/link/mail/confirm",
        data={"mail": MAIL, "code": code},
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert BOT_UNREACHABLE_ERROR in response.text
    assert EMPTY_LIST_CARD not in response.text
    # Ничего не привязалось: отказ — не «привязано» и не «занято другим».
    assert api.find_by_id(account.id).mail is None

    # Код израсходован проверкой, поэтому шаг кода снят: человек возвращается к
    # форме адреса и просит письмо заново, а не набирает то, чего не примут.
    bot.offline.clear()
    again = client.get("/account", follow_redirects=False)
    assert again.status_code == 200
    assert "Код отправлен на" not in again.text
    assert "на неё придёт код подтверждения" in again.text


def test_refusal_screen_does_not_claim_there_are_no_subscriptions() -> None:
    """Отказ и «Подписок нет» на одном экране не встречаются (#49).

    Это утверждение про сам рендер, и оно — корень той же находки: экран отказа
    зовётся с пустым списком (список-то как раз и не получен), а пустой список без
    отказа рисует честную пустоту (W4.3). Рядом они дают прямое противоречие:
    «не удалось получить список» и «подписок нет» — про одно и то же, и второе
    человек читает как ответ, после которого незачем обновлять страницу.

    Обе половины в одном тесте намеренно: правило здесь — **развилка**, и
    проверять её надо с обеих сторон. Сними развилку или сделай её безусловной —
    покраснеет один из двух наборов.
    """
    refusal = render_subscriptions([], error=SUBSCRIPTIONS_ERROR)

    assert SUBSCRIPTIONS_ERROR in refusal
    assert EMPTY_LIST_CARD not in refusal
    # Без отказа пустота остаётся честным состоянием экрана (W4.3).
    assert EMPTY_LIST_CARD in render_subscriptions([])


# --- 3. недоступность бота на входе ----------------------------------------


def test_login_survives_a_bot_outage() -> None:
    """Код верен, а хранилище не отвечает: отказ на входе, сессии нет.

    Вход по почте — главная дверь кабинета, и первое, что за ней происходит, —
    чтение аккаунта у бота (`find_by_mail`). Недоступность бота здесь не должна
    выглядеть ни как «код не подошёл», ни как сбой сервера: код человек ввёл
    верный, а не дозвонился кабинет.

    Экран — форма входа, а не ввод кода: код к этому моменту уже погашен проверкой
    (`AuthMail.verify`, код одноразовый), и поле ввода предложило бы ввести то,
    чего больше не примут. Текст при этом говорит запросить код заново — то есть
    называет действие, которое действительно сработает.
    """
    api = FakeApi()
    bot = FlakyBot(api)
    sender = FakeMailSender()
    client = TestClient(_app_over(bot, sender), raise_server_exceptions=False)

    # Код запрашивается при живом боте: упасть он должен на чтении аккаунта, а не
    # на отправке письма.
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    code = _CODE_RE.search(sender.sent_to(MAIL)[-1]["body"]).group(1)

    bot.offline.add("all")
    response = client.post(
        "/login/waiting",
        data={"mail": MAIL, "code": code},
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert LOGIN_BOT_UNREACHABLE in response.text
    # Адрес подставлен в форму: человеку не нужно набирать его заново, чтобы
    # запросить новый код.
    assert MAIL in response.text
    assert COOKIE_NAME not in response.cookies
