"""issue #65: привязка Telegram к аккаунту кабинета доводится до конца.

Дефект был не в разметке и не в связывании: `Linking.link_telegram` умел всё —
проверить `chat_id` как подтверждённый ботом и привязать только на пустое место
(§3.3), — но **флоу привязки в кабинете не существовало**. Кнопка «Привязать
Telegram» показывала текст «откройте бота и подтвердите вход», а идти в бота было
не с чем: для привязки нужен челлендж в режиме `link`, иначе бот завёл бы новый
аккаунт под свежий `chat_id`, и привязка конфликтовала бы сама с собой.

Что проверяется и почему именно так:

- **Флоу идёт тем же челленджем, что вход, но с режимом `link`.** Кабинет шлёт
  боту `mode="link"`; ссылку на бота собирает бот. `chat_id` в ответе опроса
  приходит **без** `session_token` и **без** `account_id` — привязка не создаёт
  аккаунт и не выдаёт сессию.
- **`chat_id` приходит только из ответа бота.** Ни форма старта, ни форма
  проверки не несут `chat_id`, а поле, подставленное в тело запроса, ни на что не
  влияет: привязан будет `chat_id`, подтверждённый ботом (§3.3, класс
  CVE-2026-53516 / nOAuth).
- **Разные отказы — разные экраны.** «Занят другим аккаунтом» (`chat_id_taken`)
  требует своего текста: человек должен знать, что делать. «Отклонено»,
  «код не подошёл», «истекло», «сервис не отвечает» — тоже своими.

Успех и отказы водятся **по разметке** (`tests/markup.py`), а не по адресам,
собранным руками: тест повторяет путь человека — кнопка на `/account` → экран
челленджа → проверка → «Telegram привязан».

Зависимости подставные: ни сети, ни базы (W0–W5).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from fastapi.testclient import TestClient

from cabinet.app import COOKIE_NAME, build_app
from cabinet.bot_client import TelegramAuthClient
from cabinet.mail_sender import MailSender
from tests import markup
from tests.fakes import FakeApi, FakeMailSender

# Адрес публичного API бота — loopback: клиентский API слушает там же. Порт
# значения не имеет — соединение подменено транспортом.
BASE = "http://127.0.0.1:9"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
CHAT_ID = 987_654_321
OTHER_CHAT_ID = 111_222_333
MAIL = "user@example.com"

# Адреса и тексты записаны строками, а не взяты из `cabinet.app`: на момент RED
# части имён ещё нет, и импорт уронил бы **весь файл** на сборе.
_LINK_START = "/account/link/telegram"
_LINK_VERIFY = "/account/link/telegram/verify"
DONE_TEXT = "Telegram привязан"
TAKEN_TEXT = "Этот Telegram уже привязан к другому аккаунту"


class FakePublicBot:
    """Подставной публичный API бота: `/api/v1/auth/link` и `/api/v1/auth/poll`.

    Решения опроса задаются очередью (`poll_script`): `pending`, `denied`,
    `confirmed` или отказ-ошибка. Подтверждение отдаётся формой **режима
    привязки** — `{"status": "confirmed", "chat_id": ...}`, без `session_token`
    и без `account_id`: привязка не создаёт аккаунт (issue #65). `chat_id`
    подтверждения задаётся полем `chat_id`, чтобы тест мог подсунуть «чужой».
    """

    def __init__(self, api: FakeApi) -> None:
        self.api = api
        self.link_calls: list[dict[str, Any]] = []
        self.poll_calls: list[dict[str, Any]] = []
        self.poll_script: list[dict[str, Any]] = []
        self.fail_with: Exception | None = None
        self.chat_id: int = CHAT_ID

    def handler(self, request: httpx.Request) -> httpx.Response:
        if self.fail_with is not None:
            raise self.fail_with

        path = request.url.path
        body = json.loads(request.content) if request.content else {}

        if request.method == "POST" and path == "/api/v1/auth/link":
            self.link_calls.append(body)
            code = body["public_code"]
            # Ссылку строит бот: именем бота владеет он. Префикс — как в
            # контракте привязки (`link_<код>`); сам адрес зарезервирован.
            prefix = "link" if body.get("mode") == "link" else "login"
            return httpx.Response(
                201,
                json={
                    "public_code": code,
                    "deep_link": f"https://example.invalid/bot?start={prefix}_{code}",
                    "expires_at": (NOW + timedelta(minutes=5)).isoformat(),
                },
            )

        if request.method == "POST" and path == "/api/v1/auth/poll":
            self.poll_calls.append(body)
            return self._poll_response(body)

        return httpx.Response(404, json={"error": {"code": "not_found"}})

    def _poll_response(self, _body: dict[str, Any]) -> httpx.Response:
        decision = self.poll_script.pop(0) if self.poll_script else {"status": "pending"}
        if "error" in decision:
            code, status = decision["error"]
            return httpx.Response(status, json={"error": {"code": code, "retryable": False}})
        if decision["status"] == "confirmed":
            # Форма привязки: только `chat_id`. Ни токена, ни `account_id`.
            return httpx.Response(
                200,
                json={"status": "confirmed", "chat_id": decision.get("chat_id", self.chat_id)},
            )
        return httpx.Response(200, json=decision)


def _client(bot: FakePublicBot, api: FakeApi) -> TestClient:
    transport = httpx.MockTransport(bot.handler)
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=FakeMailSender()),
        now=lambda: NOW,
        telegram_auth=TelegramAuthClient(
            base_url=BASE,
            client=httpx.Client(base_url=BASE, transport=transport),
        ),
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.app_state = app.state  # type: ignore[attr-defined]
    return client


def _signed_in_mail_only(client: TestClient, api: FakeApi) -> int:
    """Почтовый аккаунт — тот самый, которому привязка Telegram и нужна."""
    account = api.create_account(mail=MAIL, mail_verified_at=NOW)
    client.cookies.set(COOKIE_NAME, client.app_state.sessions.sign_in(account.id).token)  # type: ignore[attr-defined]
    return account.id


def _account_html(client: TestClient) -> str:
    return client.get("/account").text


def _start_link(client: TestClient) -> str:
    """Нажать «Привязать Telegram» **по разметке** — как человек.

    Адрес берётся из формы экрана, а не из константы: тест обязан упасть, если
    форма ведёт не туда, а не подставить адрес сам.
    """
    start_form = markup.form(_account_html(client), _LINK_START)
    response = client.post(str(start_form["action"]), data={}, follow_redirects=False)
    assert response.status_code == 200
    return response.text


def _verify_html(client: TestClient, challenge_html: str, code: str = "123456") -> str:
    """Проверить привязку **по разметке**: поля берутся из формы экрана.

    `public_code` едет скрытым полем, как его положил сервер; тест добавляет
    только код из бота. Ни `chat_id`, ни аккаунт сюда не подставляются.
    """
    verify_form = markup.form(challenge_html, _LINK_VERIFY)
    fields = dict(verify_form["fields"])  # type: ignore[arg-type]
    fields["code"] = code
    response = client.post(str(verify_form["action"]), data=fields, follow_redirects=False)
    assert response.status_code == 200
    return response.text


# --- 1. старт привязки: режим link, ссылка, но не секрет -------------------


def test_start_link_asks_the_bot_in_link_mode() -> None:
    """Кабинет просит у бота ссылку **в режиме привязки**, а не входа.

    Это и есть отличие от входа: в режиме `link` бот подтверждает `chat_id`, не
    создавая аккаунт. Попроси кабинет обычный `login` — бот завёл бы новый
    аккаунт под свежий `chat_id`, и привязка конфликтовала бы сама с собой.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    _signed_in_mail_only(client, api)

    _start_link(client)

    assert len(bot.link_calls) == 1
    sent = bot.link_calls[0]
    assert sent.get("mode") == "link"
    # Старт не заявляет личность: ни `chat_id`, ни аккаунта (§3.3).
    assert set(sent) == {"public_code", "secret_hash", "mode"}


def test_start_link_shows_the_bot_link_and_keeps_the_secret_out_of_the_page() -> None:
    """Экран челленджа несёт ссылку бота и поле кода; `secret` не течёт.

    Секрет берётся из памяти кабинета и обязан отсутствовать в разметке: он даёт
    завершить привязку, и утёкший секрет — это привязка чужого `chat_id` к твоему
    аккаунту. В ссылку и в скрытое поле уходит только `public_code`.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    _signed_in_mail_only(client, api)

    html = _start_link(client)

    public_code = bot.link_calls[0]["public_code"]
    deep_link = f"https://example.invalid/bot?start=link_{public_code}"
    assert deep_link in markup.hrefs(html, "Открыть Telegram")
    # Скрёб самого кода: `public_code` — то, по чему кабинет находит секрет.
    assert markup.form(html, _LINK_VERIFY)["fields"]["public_code"] == public_code  # type: ignore[index]
    secret = client.app_state.telegram_login.secret_for(public_code)  # type: ignore[attr-defined]
    assert secret not in html


def test_bot_link_opens_in_a_new_tab_and_keeps_the_cabinet_tab_alive() -> None:
    """Ссылка в бота — новой вкладкой, как на экране входа, и по той же причине.

    Челлендж начат **здесь**, а код из бота вводят здесь же по возвращении.
    Переход в той же вкладке унёс бы экран челленджа, и вернуться можно было бы
    только историей — а она начинает **другой** челлендж. `noopener` не даёт
    открытой странице подменить вкладку кабинета через `window.opener`;
    `noreferrer` не отдаёт боту адрес кабинета.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    _signed_in_mail_only(client, api)

    html = _start_link(client)

    public_code = bot.link_calls[0]["public_code"]
    deep_link = f"https://example.invalid/bot?start=link_{public_code}"
    anchor = _anchor_for(html, deep_link)
    assert 'target="_blank"' in anchor
    rel = anchor.split('rel="')[1].split('"')[0]
    assert "noopener" in rel
    assert "noreferrer" in rel


def _anchor_for(html: str, href: str) -> str:
    """Тег `<a>` с этим адресом — атрибуты проверяются на самом теге.

    `target="_blank"` на любой другой ссылке экрана ничего не говорил бы о
    ссылке в бота, и тест прошёл бы, оставаясь слепым к тому, ради чего написан.
    """
    import re

    match = re.search(rf'<a\b[^>]*href="{re.escape(href)}"[^>]*>', html)
    assert match is not None, "на экране нет ссылки в бота"
    return match.group(0)


# --- 2. успех: привязка доведена до конца по разметке ----------------------


def test_confirming_in_the_bot_links_telegram() -> None:
    """Полный путь по разметке: кнопка → челлендж → код → «Telegram привязан».

    Сверяется **запись**, а не ответ 200: тест, довольный кодом ответа, зеленел
    бы и на роуте, который рисует экран и ничего не привязывает. Почта остаётся
    нетронутой — привязка добавляет способ входа, а не заменяет его.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "confirmed", "chat_id": CHAT_ID})
    html = _verify_html(client, challenge, code="123456")

    assert DONE_TEXT in html
    after = api.find_by_id(account_id)
    assert after.chat_id == CHAT_ID
    assert after.mail == MAIL


def test_confirming_does_not_issue_a_session() -> None:
    """Привязка не выдаёт сессию: режим `link` её не возвращает, и кабинет её не шьёт.

    Привязка — не вход. Человек уже вошёл; привязка лишь добавляет способ. Новый
    cookie сессии означал бы, что кабинет вывел вход из подтверждения привязки —
    то есть повторил бы ошибку, ради которой режим `link` и введён.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "confirmed", "chat_id": CHAT_ID})
    before = dict(client.cookies)
    verify_form = markup.form(challenge, _LINK_VERIFY)
    fields = dict(verify_form["fields"])  # type: ignore[arg-type]
    fields["code"] = "123456"
    response = client.post(str(verify_form["action"]), data=fields, follow_redirects=False)

    assert COOKIE_NAME not in response.cookies
    assert dict(client.cookies) == before


# --- 3. чужой `chat_id` отвергается честным текстом ------------------------


def test_foreign_chat_id_is_refused_with_an_honest_text() -> None:
    """`chat_id`, уже привязанный к другому аккаунту, — свой текст, а не «не удалось».

    Человек доказал владение этим Telegram (бот подтвердил), поэтому скрывать за
    общим «не удалось» нечего: ему нужно знать, что этот Telegram уже занят
    другим аккаунтом. Чужой аккаунт при этом не тронут, а свой остаётся пустым.
    """
    api = FakeApi()
    victim = api.create_account(chat_id=OTHER_CHAT_ID)
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "confirmed", "chat_id": OTHER_CHAT_ID})
    html = _verify_html(client, challenge)

    assert TAKEN_TEXT in html
    assert api.find_by_id(account_id).chat_id is None
    assert api.find_by_id(victim.id).chat_id == OTHER_CHAT_ID


# --- 4. `chat_id` из формы не принимается ничем ----------------------------


def test_verify_form_carries_no_chat_id_field() -> None:
    """В форме проверки нет поля `chat_id` — личность браузер не заявляет."""
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    _signed_in_mail_only(client, api)

    challenge = _start_link(client)

    fields = markup.form(challenge, _LINK_VERIFY)["fields"]
    assert set(fields) == {"public_code", "code"}  # type: ignore[arg-type]


def test_chat_id_planted_in_the_request_is_ignored() -> None:
    """`chat_id`, подставленный в тело проверки, ни на что не влияет.

    Привязан будет `chat_id` **от бота**, а не тот, что прислал браузер: иначе
    знание чужого `chat_id` давало бы привязку чужого Telegram (§3.3).
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "confirmed", "chat_id": CHAT_ID})
    verify_form = markup.form(challenge, _LINK_VERIFY)
    fields = dict(verify_form["fields"])  # type: ignore[arg-type]
    fields["code"] = "123456"
    fields["chat_id"] = str(OTHER_CHAT_ID)  # попытка навязать чужой Telegram
    client.post(str(verify_form["action"]), data=fields, follow_redirects=False)

    assert api.find_by_id(account_id).chat_id == CHAT_ID


# --- 5. состояния разведены: ожидание, отказ, код, срок, недоступность -----


def test_pending_keeps_the_challenge_and_shows_waiting() -> None:
    """`pending` — ожидание, а не отказ: сессии нет, привязки нет, текст свой."""
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "pending"})
    html = _verify_html(client, challenge)

    assert "не подтверждена" in html
    assert api.find_by_id(account_id).chat_id is None


def test_denied_shows_the_refusal() -> None:
    """Человек отклонил привязку в Telegram — свой текст, привязки нет."""
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "denied"})
    html = _verify_html(client, challenge)

    assert "отклонена в Telegram" in html
    assert api.find_by_id(account_id).chat_id is None


def test_wrong_code_shows_the_code_error() -> None:
    """`nonce_mismatch` — код не подошёл: свой текст, а не «привязка отклонена»."""
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"error": ("nonce_mismatch", 409)})
    html = _verify_html(client, challenge)

    assert "Код из Telegram не подошёл" in html
    assert api.find_by_id(account_id).chat_id is None


def test_expired_operation_shows_the_expired_text() -> None:
    """Истёкший или уже использованный челлендж — начать заново, а не повторять код."""
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"error": ("operation_not_found", 404)})
    html = _verify_html(client, challenge)

    assert "истекло" in html
    assert api.find_by_id(account_id).chat_id is None


def test_bot_unreachable_shows_the_service_text() -> None:
    """Бот не ответил — причина не в человеке: текст про сервис, привязки нет."""
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    account_id = _signed_in_mail_only(client, api)

    challenge = _start_link(client)
    bot.poll_script.append({"status": "confirmed", "chat_id": CHAT_ID})
    # Подтверждение уже есть у бота; недоступность — на опросе.
    verify_form = markup.form(challenge, _LINK_VERIFY)
    bot.fail_with = httpx.ConnectError("нет связи")
    fields = dict(verify_form["fields"])  # type: ignore[arg-type]
    fields["code"] = "123456"
    response = client.post(str(verify_form["action"]), data=fields, follow_redirects=False)

    assert response.status_code == 200
    assert "не отвечает" in response.text
    assert api.find_by_id(account_id).chat_id is None


def test_verify_without_a_started_challenge_is_not_500() -> None:
    """Проверка без начатого челленджа — отказ на экране, а не 500.

    Старая вкладка или перезапуск кабинета: состояние челленджа живёт в памяти и
    теряется. Это обычное состояние, а не сбой сервера.
    """
    api = FakeApi()
    bot = FakePublicBot(api)
    client = _client(bot, api)
    _signed_in_mail_only(client, api)

    response = client.post(
        _LINK_VERIFY, data={"public_code": "нет-такого", "code": "123456"}, follow_redirects=False
    )

    assert response.status_code == 200  # не 500 и не 404
    assert "не начата" in response.text
