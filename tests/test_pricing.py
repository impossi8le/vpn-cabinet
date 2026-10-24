"""Цена покупки: формула бота, перенесённая в кабинет.

Формула не выдумана — она живёт в **репозитории бота**, в файле
`includes/pricing.py`, в функции `calculate_price`. Этот модуль кабинета —
её двойник, а таблица ниже повторяет поведение того файла, а не
представление о нём.

Почему это вообще тест на паритет, а не просто тест нашей функции: цену
называют **два канала** — бот и кабинет. Две реализации одного вопроса
«сколько платить» разойдутся при первой же правке скидок, и человек,
вошедший по почте, увидит не ту сумму, что человек в Telegram. Поэтому
здесь проверяется не «какая-то разумная цена», а **та же самая**, что
отдаёт бот на тех же входах. Ожидаемые числа сняты с прогона файла бота,
не посчитаны на бумаге.

Правка цены — это правка **обоих** файлов сразу. Тронул один — сломал
другой, и тест это скажет.
"""

from __future__ import annotations

from dataclasses import asdict

import pytest

from cabinet.pricing import (
    BUNDLE_2_DEVICES_DISCOUNT_PERCENT,
    BUNDLE_3_DEVICES_DISCOUNT_PERCENT,
    PRICES,
    REFERRAL_DISCOUNT_PERCENT,
    calculate_price,
)

# Входы → ожидаемый ответ. Поля — те же, что в словаре `calculate_price`
# бота: перенесены один в один, чтобы расхождение читалось построчно.
PARITY_CASES = [
    # --- четыре базовые цены без скидок -----------------------------------
    ({"days": 30}, (140, 140, 0, 0, 0, 140, ())),
    ({"days": 90}, (400, 400, 0, 0, 0, 400, ())),
    ({"days": 180}, (750, 750, 0, 0, 0, 750, ())),
    ({"days": 360}, (1300, 1300, 0, 0, 0, 1300, ())),
    ({"days": 30, "devices": 1}, (140, 140, 0, 0, 0, 140, ())),
    # --- комплект −10% / −15% по всем срокам ------------------------------
    (
        {"days": 30, "devices": 2},
        (140, 280, 0, 28, 0, 252, ("Комплект 2 устройства: -10%",)),
    ),
    (
        {"days": 30, "devices": 3},
        (140, 420, 0, 63, 0, 357, ("Комплект 3 устройства: -15%",)),
    ),
    (
        {"days": 90, "devices": 2},
        (400, 800, 0, 80, 0, 720, ("Комплект 2 устройства: -10%",)),
    ),
    (
        {"days": 90, "devices": 3},
        (400, 1200, 0, 180, 0, 1020, ("Комплект 3 устройства: -15%",)),
    ),
    (
        {"days": 180, "devices": 2},
        (750, 1500, 0, 150, 0, 1350, ("Комплект 2 устройства: -10%",)),
    ),
    # 2250 × 15% = 337.5 → скидка вниз (337), итог вверх (1913).
    (
        {"days": 180, "devices": 3},
        (750, 2250, 0, 337, 0, 1913, ("Комплект 3 устройства: -15%",)),
    ),
    (
        {"days": 360, "devices": 2},
        (1300, 2600, 0, 260, 0, 2340, ("Комплект 2 устройства: -10%",)),
    ),
    (
        {"days": 360, "devices": 3},
        (1300, 3900, 0, 585, 0, 3315, ("Комплект 3 устройства: -15%",)),
    ),
    # --- реферальный код −5% ----------------------------------------------
    (
        {"days": 30, "referral_code": "ABC"},
        (140, 140, 7, 0, 0, 133, ("Реферальный код: -5%",)),
    ),
    (
        {"days": 360, "devices": 3, "referral_code": "ABC"},
        (1300, 3900, 165, 585, 0, 3150, ("Комплект 3 устройства: -15%", "Реферальный код: -5%")),
    ),
    # --- промокод ---------------------------------------------------------
    (
        {"days": 30, "promo_percent": 10},
        (140, 140, 0, 0, 14, 126, ("Промокод: -10%",)),
    ),
    # 720 × 7% = 50.4 → скидка вниз (50), итог 670.
    (
        {"days": 90, "devices": 2, "promo_percent": 7},
        (400, 800, 0, 80, 50, 670, ("Комплект 2 устройства: -10%", "Промокод: -7%")),
    ),
    # Промокод без процента (0) и отрицательный — скидки нет.
    ({"days": 30, "promo_percent": 0}, (140, 140, 0, 0, 0, 140, ())),
    ({"days": 30, "promo_percent": -5}, (140, 140, 0, 0, 0, 140, ())),
    # --- промокод и реферал вместе ----------------------------------------
    # Осознанное решение следовать коду бота: скидки СУММИРУЮТСЯ, каждая от
    # цены после комплекта. Спека кабинета (`docs/2026-10-17-purchase-flow-design.md`
    # §2) требует брать большую — расхождение заведено отдельно, чинить
    # должен владелец, а не эта копия. Разойтись с ботом канал не имеет права.
    (
        {"days": 30, "referral_code": "ABC", "promo_percent": 10},
        (140, 140, 7, 0, 14, 119, ("Промокод: -10%", "Реферальный код: -5%")),
    ),
    (
        {"days": 180, "devices": 3, "referral_code": "ABC", "promo_percent": 10},
        (
            750,
            2250,
            95,
            337,
            191,
            1627,
            ("Комплект 3 устройства: -15%", "Промокод: -10%", "Реферальный код: -5%"),
        ),
    ),
    # --- устройства вне 1/2/3: скидки за комплект нет, но и отказа нет -----
    # Бот молча считает так же (devices=4 скидки не даёт). Добавлять отказ
    # здесь значило бы разойтись с ним на ровном месте.
    ({"days": 30, "devices": 4}, (140, 560, 0, 0, 0, 560, ())),
]


@pytest.mark.parametrize(("inputs", "expected"), PARITY_CASES)
def test_price_matches_the_bot(inputs: dict, expected: tuple):
    """Тот же вход — та же цена, что у бота, до рубля."""
    (
        base_price,
        total_base,
        referral_discount,
        bundle_discount,
        promo_discount,
        final_price,
        discounts_applied,
    ) = expected
    quote = calculate_price(**inputs)
    assert asdict(quote) == {
        "base_price": base_price,
        "total_base": total_base,
        "referral_discount": referral_discount,
        "bundle_discount": bundle_discount,
        "promo_discount": promo_discount,
        "final_price": final_price,
        "discounts_applied": discounts_applied,
    }


def test_unsupported_period_is_a_refusal_not_a_zero():
    """Неподдерживаемый срок — отказ, а не молчаливый ноль.

    Ноль на неизвестный срок читался бы как «бесплатно» и продал бы
    подписку даром. Отказ заставляет вызывающий код показать ошибку.
    """
    with pytest.raises(ValueError):
        calculate_price(days=45)


def test_refusal_names_the_period():
    """В отказе назван срок: иначе непонятно, что именно не поддержано."""
    with pytest.raises(ValueError, match="45"):
        calculate_price(days=45)


def test_every_supported_period_is_listed():
    """Множество сроков — закрытое и совпадает с ценами бота."""
    assert PRICES == {30: 140, 90: 400, 180: 750, 360: 1300}


def test_discount_percentages_match_the_bot():
    """Проценты скидок — те же, что у бота: комплект 10/15, реферал 5."""
    assert BUNDLE_2_DEVICES_DISCOUNT_PERCENT == 10
    assert BUNDLE_3_DEVICES_DISCOUNT_PERCENT == 15
    assert REFERRAL_DISCOUNT_PERCENT == 5


@pytest.mark.parametrize("referral_code", [None, ""])
def test_no_referral_code_means_no_referral_discount(referral_code):
    """Пустой или отсутствующий код скидки не даёт — как у бота,
    где скидка включается только правдивым значением кода."""
    quote = calculate_price(days=30, referral_code=referral_code)
    assert quote.referral_discount == 0
    assert "Реферальный код: -5%" not in quote.discounts_applied


def test_discounts_floor_and_total_ceils():
    """Округление: скидки вниз, итог вверх — до целых рублей.

    2250 × 15% = 337.5: скидка 337 (вниз), итог ceil(1912.5) = 1913 (вверх).
    Разница не больше рубля и всегда в пользу покупателя на итоге.
    """
    quote = calculate_price(days=180, devices=3)
    assert quote.bundle_discount == 337
    assert quote.final_price == 1913


def test_promo_and_referral_are_summed_not_replaced():
    """Спорное место: скидки складываются, а не «берётся большая».

    Спека кабинета требует максимум из двух; бот суммирует и помечает это
    в коде как намеренное. Каналы не должны называть разные суммы, поэтому
    здесь поведение бота: 140 − 14 − 7 = 119, а не 140 − 14 = 126.
    """
    quote = calculate_price(days=30, referral_code="ABC", promo_percent=10)
    assert quote.promo_discount == 14
    assert quote.referral_discount == 7
    assert quote.final_price == 119


def test_discounts_are_computed_from_price_after_bundle():
    """Промокод и реферал считаются от цены ПОСЛЕ комплекта, не от полной.

    2250 после 15% → 1912.5; промокод 10% = 191.25 → 191, реферал 5% =
    95.625 → 95. Считай их от 2250 — вышло бы 225 и 112, и цена разошлась бы
    с ботом.
    """
    quote = calculate_price(days=180, devices=3, referral_code="ABC", promo_percent=10)
    assert quote.promo_discount == 191
    assert quote.referral_discount == 95


def test_reason_lines_are_in_the_bot_order():
    """Причины скидок идут в порядке бота: комплект, промокод, реферал.
    Экран показывает этот список как есть — порядок виден человеку."""
    quote = calculate_price(days=180, devices=3, referral_code="ABC", promo_percent=10)
    assert quote.discounts_applied == (
        "Комплект 3 устройства: -15%",
        "Промокод: -10%",
        "Реферальный код: -5%",
    )
