"""Экран покупки, шаги 1–2: тариф и число устройств (#36).

Экран отвечает на один вопрос: «сколько это стоит». И отвечает он не собой, а
`cabinet.pricing` — переносом формулы бота. Цена здесь не «примерно такая»: два
канала (бот и кабинет) обязаны назвать **одну сумму**, и она берётся из того же
расчёта, а не пишется в разметке. Поэтому тест, который здесь главный,
проверяет не «на странице есть цифры», а что смена ответа калькулятора меняет
страницу: подставной расчёт с заведомо чужим числом обязан появиться в разметке,
а настоящие числа — исчезнуть.

Остальное — как у соседних экранов: без сессии редирект на вход (не 500 и не
чужой экран), и зелёного нет нигде: цена и скидка — не подтверждённая защита
(§7). 30 дней помечены честно: право на бесплатный триал проверяет бот, и
обещать «бесплатно» безусловно кабинет не может.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi.testclient import TestClient

from cabinet import render
from cabinet.app import build_app
from cabinet.auth_mail import CODE_LENGTH
from cabinet.mail_sender import MailSender
from cabinet.pricing import PriceQuote
from cabinet.theme import COLORS
from tests.fakes import FakeApi, FakeMailSender

NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
MAIL = "buyer@example.com"


def _sign_in_client(api: FakeApi) -> TestClient:
    """Клиент, вошедший по почте через настоящий `/login` — как человек."""
    sender = FakeMailSender()
    app = build_app(
        store=api,
        mail_sender=MailSender(sender="cabinet@example.com", transport=sender),
        now=lambda: NOW,
    )
    client = TestClient(app, raise_server_exceptions=False)
    client.post("/login", data={"mail": MAIL}, follow_redirects=False)
    body = sender.sent_to(MAIL)[-1]["body"]
    code = "".join(ch for ch in body if ch.isdigit())[:CODE_LENGTH]
    client.post("/login/waiting", data={"mail": MAIL, "code": code}, follow_redirects=False)
    return client


def _signed_in() -> TestClient:
    """Вошедший клиент на аккаунте без подписок: покупка — главный его случай."""
    api = FakeApi()
    api.create_account(mail=MAIL, mail_verified_at=NOW)
    return _sign_in_client(api)


def _markup_without_palette(html: str) -> str:
    """HTML без блока `:root`: зелёный в палитре — заготовка на будущее.

    Проверять надо применение цвета, а не объявление: зелёный обязан остаться в
    `theme` (причина `confirmed_protection`), но ни на одном элементе экрана его
    быть не должно.
    """
    start = html.find(":root {")
    end = html.find("}", start)
    return html[:start] + html[end:]


def _assert_no_green(html: str) -> None:
    assert COLORS.green.upper() not in _markup_without_palette(html).upper()


# --- без сессии — на вход -------------------------------------------------


def test_buy_without_session_redirects_to_login() -> None:
    """Без cookie — редирект на вход, а не 500 и не экран чужого аккаунта.

    Это обычное состояние первого захода, а не сбой: тот же контракт, что у
    `/subscriptions`. Ответ 500 означал бы, что отсутствие сессии считается
    ошибкой сервера, — и вылезал бы у каждого, кто открыл адрес.
    """
    client = TestClient(build_app(store=FakeApi()), raise_server_exceptions=False)

    response = client.get("/buy", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


# --- экран показывает цены калькулятора ----------------------------------


def test_buy_screen_shows_four_periods_with_calculator_prices() -> None:
    """Четыре срока показаны, и у каждого — цена из калькулятора, не выдуманная.

    Числа сверяются буквально: 140/400/750/1300 — это ответ `calculate_price` на
    30/90/180/360 дней (таблица паритета с ботом), а не «какие-нибудь цифры».
    Парная проверка к подставному калькулятору ниже: там эти же числа обязаны
    исчезнуть.
    """
    html = _signed_in().get("/buy").text

    assert "30 дней" in html
    assert "90 дней" in html
    assert "180 дней" in html
    assert "360 дней" in html

    assert "140\xa0₽" in html
    assert "400\xa0₽" in html
    assert "750\xa0₽" in html
    # Тысяча разбита по разрядам неразрывным пробелом — как суммы на подписках.
    assert "1\xa0300\xa0₽" in html


def test_buy_screen_shows_price_per_month() -> None:
    """Рядом с ценой — цена в месяц: так сроки сравниваются, а не запоминаются.

    Числа считает кабинет из ответа калькулятора (140 / 133 / 125 / 108), и они
    совпадают с таблицей спеки §2. Проверяются буквально, вместе с подписью
    «/мес»: без неё число в месяц неотличимо от цены за период.
    """
    html = _signed_in().get("/buy").text

    assert "140\xa0₽/мес" in html
    assert "133\xa0₽/мес" in html
    assert "125\xa0₽/мес" in html
    assert "108\xa0₽/мес" in html


def test_buy_screen_shows_three_device_options_with_bundle_savings() -> None:
    """Три варианта числа устройств, и у комплектов — экономия в рублях.

    Скидка берётся у калькулятора (`bundle_discount`), а не считается на экране:
    для 30 дней 2 устройства экономят 28 ₽, 3 — 63 ₽. Именно эти суммы.

    **Процентов здесь больше нет** — и это решение, а не пропажа: док продающего
    кабинета (§6 п. 4) просит показывать экономию в рублях, потому что «−10%»
    требует счёта, а «экономия 28 ₽» — нет. Проценты остались в текстах бота, где
    свою роль играют, а кабинет называет то, что человек получит.
    """
    html = _signed_in().get("/buy").text

    assert "1 устройство" in html
    assert "2 устройства" in html
    assert "3 устройства" in html

    # Экономия в рублях — из ответа калькулятора для выбранного срока (30 дней).
    assert "экономия 28\xa0₽" in html
    assert "экономия 63\xa0₽" in html


def test_buy_screen_shows_total_for_the_chosen_combination() -> None:
    """Итог показан для выбранной пары «срок + устройства» — из калькулятора.

    По умолчанию выбран 30-дневный тариф на одно устройство, и итог — 140 ₽.
    """
    html = _signed_in().get("/buy").text

    assert "Итого" in html
    assert "140\xa0₽" in html


def test_buy_screen_renders_through_the_real_route() -> None:
    """Экран идёт через настоящий роут `TestClient`, а не прямой вызов рендера.

    Роут может не подключаться к собранному приложению (об этом уже спотыкались:
    шаблон есть, а страница отвечает 404). Запрос проверяет проводку целиком.
    """
    response = _signed_in().get("/buy")

    assert response.status_code == 200
    assert "Выберите тариф" in response.text
    assert "140\xa0₽" in response.text


# --- цены считаются, а не вписаны ----------------------------------------


def test_prices_come_from_the_calculator_not_baked_into_the_template() -> None:
    """Подмена калькулятора меняет страницу — значит, цена считается на месте.

    Главная проверка задачи. Подставной расчёт отдаёт заведомо чужое число
    (424 242 ₽); оно обязано появиться в разметке, а настоящие числа
    калькулятора (140 ₽ и т. д.) — исчезнуть. Тест, который просто ищет «₽» на
    странице, зеленел бы и на цене, вписанной в шаблон руками.
    """
    seen: list[tuple[int, int]] = []

    def stub(days: int, devices: int = 1, referral_code: str | None = None, promo_percent: int = 0):
        seen.append((days, devices))
        return PriceQuote(
            base_price=424242,
            total_base=424242 * devices,
            referral_discount=0,
            bundle_discount=4242,
            promo_discount=0,
            final_price=424242,
            discounts_applied=(),
        )

    html = render.render_buy(calculator=stub)

    assert "424\xa0242\xa0₽" in html
    assert "4\xa0242\xa0₽" in html
    # Настоящих чисел калькулятора на странице нет: они пришли бы только из
    # вписанной руками цены.
    assert "140\xa0₽" not in html
    assert "750\xa0₽" not in html
    # Калькулятор спрашивали про все четыре срока и все три числа устройств.
    assert {days for days, _ in seen} == {30, 90, 180, 360}
    assert {devices for _, devices in seen} >= {1, 2, 3}


# --- выбор и переходы -----------------------------------------------------


def test_buy_screen_reflects_the_chosen_period_and_devices() -> None:
    """Адрес задаёт выбор: `?days=180&devices=3` показывает итог этой пары.

    Это и есть «цена обновляется»: без JavaScript выбор выражается ссылкой, а
    пересчёт делает сервер тем же калькулятором. Для 180 дней и 3 устройств итог
    1913 ₽ — из таблицы паритета (скидка вниз 337, итог вверх).
    """
    html = _signed_in().get("/buy?days=180&devices=3").text

    assert "1\xa0913\xa0₽" in html


def test_buy_screen_ignores_unknown_selection() -> None:
    """Неизвестный срок в адресе не роняет экран и не продаёт даром.

    `calculate_price` на срок вне `PRICES` бросает `ValueError`; роут обязан
    подставить умолчание, а не отдать 500 и не показать ноль.
    """
    response = _signed_in().get("/buy?days=45&devices=9")

    assert response.status_code == 200
    assert "140\xa0₽" in response.text  # умолчание — 30 дней, одно устройство


# --- триал помечен честно -------------------------------------------------


def test_free_access_is_named_with_the_check_a_click_away() -> None:
    """«Бесплатно» возвращается на экран — вместе с проверкой права за один клик.

    Здесь стояло обратное правило: слова «бесплатно» на экране быть не должно,
    потому что кабинет не умел проверять право на триал. Теперь умеет — проверка
    стоит на шаге подтверждения, — и док продающего кабинета требует назвать
    бесплатный доступ **первым**, что видит человек (§1, §6 п. 2), а не прятать
    его среди платных тарифов.

    Цена этого возврата — признак `free` в переходе: по нему следующий экран
    покажет право, а шаг оплаты не возьмёт денег. Без него «бесплатно» было бы
    словом на экране и ничем больше.
    """
    html = _signed_in().get("/buy").text

    assert "30 дней бесплатно" in html
    assert "Пробный период" in html
    assert "free=1" in html


# --- цвет и переходы со связанных экранов --------------------------------


def test_buy_screen_has_no_green() -> None:
    """Цена и скидка — не защита (§7): зелёного на экране покупки нет.

    Дешевле выбранный тариф не значит «защищено»; зелёный в проекте отдан
    одному смыслу — подтверждённому замеру, которого здесь не делают.
    """
    html = _signed_in().get("/buy").text

    _assert_no_green(html)


def test_buy_screen_links_back_to_subscriptions() -> None:
    """С экрана покупки есть возврат к подпискам — шаг адресуем, а не тупик."""
    html = _signed_in().get("/buy").text

    assert 'href="/subscriptions"' in html


def test_subscriptions_screen_links_to_buy() -> None:
    """На экран покупки попадают с экрана подписок: ссылка обязана быть там.

    Без неё экран существует по прямому адресу, и «достижим» держится только на
    знании адреса — то есть не держится вовсе.
    """
    html = _signed_in().get("/subscriptions").text

    assert 'href="/buy"' in html


def test_subscriptions_screen_links_to_the_account_screen() -> None:
    """С главного экрана есть вход в способы входа.

    Экран аккаунта со всей привязкой и отвязкой почты и Telegram существовал, а
    ссылки на него не было нигде, кроме углового состояния оплаты: попасть туда
    можно было только зная адрес. Нашёл сквозной прогон
    (`test_e2e_journey.py`) — на этом тесты экранов бессильны, каждый из них
    зелёный в отдельности.
    """
    html = _signed_in().get("/subscriptions").text

    assert 'href="/account"' in html


def test_buy_screen_leads_to_the_confirmation_step() -> None:
    """С шага «тариф и устройства» есть переход на подтверждение — с той же парой.

    Без него шаги 3–5 существуют, а путь обрывается: человек выбирает тариф и
    дальше попадает только по прямому адресу. Адрес перехода несёт выбранную
    пару, иначе подтверждение показало бы не то, что человек выбрал.
    """
    html = _signed_in().get("/buy?days=180&devices=2").text

    assert 'href="/buy/confirm?days=180&amp;devices=2"' in html
