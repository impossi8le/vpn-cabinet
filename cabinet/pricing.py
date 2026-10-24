"""Цена покупки: сколько стоит тариф в кабинете.

Один вопрос — один ответ. Цену называет и бот, и кабинет, и это **одна и та
же** цена: файл переносит расчёт из репозитория бота (файл `includes/pricing.py`,
функция `calculate_price`) один в один, а тест `tests/test_pricing.py` держит
их вместе. Считай кабинет по-своему — человек, вошедший по почте, увидел бы
не ту сумму, что человек в Telegram, и правка скидки в одном месте молча
разошлась бы с другим.

Модуль чистый: ни HTTP, ни базы, ни шаблонов. Экраны покупки кладутся сверху
на эти числа (следующие задачи), а не считают рядом своё.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "BUNDLE_2_DEVICES_DISCOUNT_PERCENT",
    "BUNDLE_3_DEVICES_DISCOUNT_PERCENT",
    "PRICES",
    "REFERRAL_DISCOUNT_PERCENT",
    "PriceQuote",
    "calculate_price",
]

# Базовые цены за одно устройство, в рублях. Значения — из бота; здесь они
# не «примерные», а точные: разойдись хоть одно — разошлась бы цена канала.
PRICES = {
    30: 140,
    90: 400,
    180: 750,
    360: 1300,
}

# Скидки в процентах.
REFERRAL_DISCOUNT_PERCENT = 5
BUNDLE_2_DEVICES_DISCOUNT_PERCENT = 10
BUNDLE_3_DEVICES_DISCOUNT_PERCENT = 15

# Сколько устройств даёт скидку за комплект. Ровно 2 и 3: как у бота — иного
# числа устройств комплект не образует, и отказа на него тоже нет.
_BUNDLE_DISCOUNTS = {
    2: BUNDLE_2_DEVICES_DISCOUNT_PERCENT,
    3: BUNDLE_3_DEVICES_DISCOUNT_PERCENT,
}


@dataclass(frozen=True)
class PriceQuote:
    """Разобранная цена: сколько просим и из чего это вышло.

    Поля повторяют ответ бота один в один, чтобы сверка в тесте читалась
    построчно и любое расхождение было видно сразу.
    """

    base_price: int
    """Базовая цена за одно устройство, без скидок."""
    total_base: int
    """Цена за все устройства до скидок: `base_price × devices`."""
    referral_discount: int
    """Скидка по реферальному коду, в рублях (округляется вниз)."""
    bundle_discount: int
    """Скидка за комплект, в рублях (округляется вниз)."""
    promo_discount: int
    """Скидка по промокоду, в рублях (округляется вниз)."""
    final_price: int
    """Итог к оплате, в рублях (округляется вверх)."""
    discounts_applied: tuple[str, ...] = ()
    """Применённые скидки строками для чека — в порядке бота."""


def calculate_price(
    days: int,
    devices: int = 1,
    referral_code: str | None = None,
    promo_percent: int = 0,
) -> PriceQuote:
    """Посчитать цену так же, как её считает бот.

    Входы — срок в днях, число устройств, реферальный код (правдивое значение
    значит «применяется») и процент промокода (0 — без промокода). Выход —
    `PriceQuote`.

    Неподдерживаемый срок — отказ (`ValueError`), а не ноль: ноль на
    неизвестный срок продал бы подписку даром.
    """
    if days not in PRICES:
        raise ValueError(f"Неподдерживаемый период: {days} дней")

    base_price = PRICES[days]
    total_base = base_price * devices

    discounts_applied: list[str] = []
    referral_discount = 0.0
    bundle_discount = 0.0
    promo_discount = 0.0

    # Скидка за комплект: только 2 и 3 устройства. Прочее — без скидки, без
    # отказа: так же ведёт себя бот.
    bundle_percent = _BUNDLE_DISCOUNTS.get(devices)
    if bundle_percent is not None:
        bundle_discount = total_base * (bundle_percent / 100)
        discounts_applied.append(f"Комплект {devices} устройства: -{bundle_percent}%")

    price_after_bundle = total_base - bundle_discount

    # Промокод и реферальный код **суммируются**: это разные акции — скидка
    # по коду друга не должна сгорать из-за промокода. Обе считаются от цены
    # после комплекта, чтобы скидки не начислялись друг на друга.
    #
    # Здесь код кабинета следует боту вопреки спеке
    # `docs/2026-10-17-purchase-flow-design.md` §2, которая требует брать из
    # двух скидок большую, а не складывать. Бот суммирует и помечает это в
    # своём коде как намеренное. Два канала не должны называть разные суммы,
    # поэтому верен код бота; расхождение со спекой заведено владельцу
    # отдельно — «чинить» его здесь значит разойтись с ботом на деньгах.
    if promo_percent > 0:
        promo_discount = price_after_bundle * (promo_percent / 100)
        discounts_applied.append(f"Промокод: -{promo_percent}%")
    if referral_code:
        referral_discount = price_after_bundle * (REFERRAL_DISCOUNT_PERCENT / 100)
        discounts_applied.append(f"Реферальный код: -{REFERRAL_DISCOUNT_PERCENT}%")

    # Округление как у бота: до целых рублей, потому что дробные копейки в
    # чеке выглядят как ошибка. Скидки вниз, итог вверх — разница не больше
    # рубля и остаётся на стороне покупателя.
    referral_discount = math.floor(referral_discount)
    bundle_discount = math.floor(bundle_discount)
    promo_discount = math.floor(promo_discount)
    final_price = math.ceil(price_after_bundle - promo_discount - referral_discount)

    return PriceQuote(
        base_price=base_price,
        total_base=total_base,
        referral_discount=referral_discount,
        bundle_discount=bundle_discount,
        promo_discount=promo_discount,
        final_price=final_price,
        discounts_applied=tuple(discounts_applied),
    )
