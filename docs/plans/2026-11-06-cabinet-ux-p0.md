# План: P0-правки пути в кабинете

> **Для агентов:** обязательный подключённый навык —
> `superpowers:subagent-driven-development` либо `superpowers:executing-plans`.
> Шаги отмечены `- [ ]`.

**Цель:** убрать четыре трения, из-за которых человек теряет путь: не видит, где
он в покупке; не знает, когда сбросится лимит региона; не понимает, что аккаунт
заводится сам; не знает, что делать, если письмо не пришло.

**Архитектура:** правки локальны экранам. Три из четырёх — разметка и тексты на
существующих роутах, четвёртая (прогресс шагов) — общий фрагмент разметки,
подключаемый четырьмя шаблонами покупки. Данные не меняются: всё нужное уже
приходит от бота (`limits.reset_at`) или известно из адреса (`days`, `devices`).

**Стек:** Python 3.11, FastAPI, Jinja2, pytest, ruff.

**Разбор:** [модель пути и слабые места](../2026-11-06-cabinet-ux-model.md) §6,
пункты P0 1–4.

## Общие ограничения

- Кабинет **не ходит в базу** — только внутренний HTTP к боту.
- **Зелёный — только подтверждённая защита**; прогресс и подсказки нейтральны.
- Тексты — на русском, в тоне проекта: без обещаний и без «не удалось».
- Тесты пишутся раньше кода; каждый краснеет без правки.
- Публичность: никаких адресов и хостов в коде и тестах.

## Review Focus

- **Человек попал на шаг покупки по прямому адресу** (без `days`/`devices`) —
  прогресс не должен показывать «шаг 3 из 4» там, где контекста нет: подпись
  обязана исчезать, а не врать.
- **Лимит без даты сброса** (`reset_at` пуст или неизвестного формата) — подпись
  должна остаться читаемой, без «None» и пустых скобок.
- **Письмо приходит, но код не подходит** — подсказка про спам не должна
  выглядеть как причина отказа; она рядом, а не вместо причины.
- **Смена адреса на экране кода** — переход обязан вести на форму входа и
  сохранять уже введённый адрес, иначе человек набирает его заново.
- **Экран с кодом на телефоне** — подсказка не должна отодвигать поле и кнопку
  ниже сгиба.

---

### Task 1: Прогресс шагов покупки

**Файлы:**
- Создать: `cabinet/templates/_buy_steps.html`
- Изменить: `cabinet/templates/buy.html`, `buy_confirm.html`, `buy_servers.html`, `buy_pay.html`
- Тест: `tests/test_buy_steps.py`

**Интерфейсы:**
- Отдаёт: фрагмент принимает `step` (int 1–4) и `steps_total` (int, по умолчанию 4);
  при `step` вне диапазона не рисуется ничего.
- Использует: ничего из соседних задач.

- [ ] **Step 1: Написать падающий тест**

```python
def test_buy_screens_show_step_progress():
    api = FakeApi()
    account = api.create_account(chat_id=555_000_111)
    client = _client_for(api, account.id)

    assert "Шаг 1 из 4" in client.get("/buy?days=30&devices=1").text
    assert "Шаг 2 из 4" in client.get("/buy/confirm?days=30&devices=1").text
    assert "Шаг 3 из 4" in client.get("/buy/servers?days=30&devices=1").text
    assert "Шаг 4 из 4" in client.get("/buy/pay?days=30&devices=1").text
```

Пара: на `/buy` без `days` прогресса нет — `assert "Шаг" not in html`.

- [ ] **Step 2: Прогнать тест — убедиться, что падает**

Запуск: `python -m pytest tests/test_buy_steps.py -v`
Ожидание: FAIL — подписи шагов нет ни на одном экране.

- [ ] **Step 3: Сделать фрагмент `_buy_steps.html` и подключить**

Фрагмент рисует `<p class="group-t">Шаг {{ step }} из {{ steps_total }}</p>`,
только если `1 <= step <= steps_total`. Подключение — `{% include "_buy_steps.html" %}`
в четырёх шаблонах сразу под `.nav`, с `{% set step = 1 %}` и так далее перед ним.

- [ ] **Step 4: Прогнать тест — убедиться, что проходит**

Запуск: `python -m pytest tests/test_buy_steps.py -v`
Ожидание: PASS. Затем весь набор: `python -m pytest -q`.

- [ ] **Step 5: Коммит**

```bash
git add cabinet/templates/_buy_steps.html cabinet/templates/buy.html cabinet/templates/buy_confirm.html cabinet/templates/buy_servers.html cabinet/templates/buy_pay.html tests/test_buy_steps.py
git commit -m "Покупка: видно, где человек и сколько осталось"
```

---

### Task 2: Дата сброса лимита смены региона

**Файлы:**
- Изменить: `cabinet/render.py`, `cabinet/templates/region.html`
- Тест: `tests/test_region.py` (дополнить)

**Интерфейсы:**
- Отдаёт: `format_reset_date(value: str | None) -> str` — «9 ноября» из
  ISO-строки; пустая строка, если значения нет или оно не разобрано.
- Использует: `limits.reset_at` из ответа бота (ISO-строка, может отсутствовать).

- [ ] **Step 1: Написать падающий тест**

```python
def test_region_shows_when_the_limit_resets(telegram_account):
    api, account = telegram_account
    api.reissue_payload["limits"] = {
        "allowed": 2,
        "used": 2,
        "remaining": 0,
        "window_days": 7,
        "reset_at": "2026-11-09T09:09:57Z",
    }

    html = _client(api, account.id).get("/region").text

    assert "9 ноября" in html
```

Пара: `reset_at` отсутствует — на экране нет «None» и пустых скобок.

- [ ] **Step 2: Прогнать — падает**

Запуск: `python -m pytest tests/test_region.py::test_region_shows_when_the_limit_resets -v`
Ожидание: FAIL — даты на экране нет.

- [ ] **Step 3: Показать дату в шаблоне**

Разбор даты живёт в `cabinet/render.py` (`format_reset_date`), в шаблон приходит
готовой строкой: разметка не занимается разбором дат. Пустая строка — строка в
шаблоне не рисуется вовсе.

- [ ] **Step 4: Прогнать — проходит, затем весь набор**

Запуск: `python -m pytest tests/test_region.py -v` → PASS; `python -m pytest -q` → зелёный.

- [ ] **Step 5: Коммит**

```bash
git add cabinet/render.py cabinet/templates/region.html tests/test_region.py
git commit -m "Смена региона: сказано, когда сбросится лимит"
```

---

### Task 3: На входе — что аккаунт заведётся сам

**Файлы:**
- Изменить: `cabinet/templates/login.html`
- Тест: `tests/test_login_screens.py` (дополнить)

**Интерфейсы:** правка только текста; контекст шаблона не меняется.

- [ ] **Step 1: Написать падающий тест**

```python
def test_login_says_the_account_is_created_automatically(login_html: str) -> None:
    """Человек не должен гадать, вход это или регистрация."""
    assert "заведётся сам" in login_html
```

- [ ] **Step 2: Прогнать — падает**

Запуск: `python -m pytest tests/test_login_screens.py -k created_automatically -v`
Ожидание: FAIL.

- [ ] **Step 3: Добавить строку**

Одна строка под `lead`: «Аккаунт заведётся сам — почту подтверждать заранее не
нужно». Тон — как у соседних подписей, без обещаний.

- [ ] **Step 4: Прогнать — проходит; весь набор**

Запуск: `python -m pytest tests/test_login_screens.py -v` → PASS; `python -m pytest -q`.

- [ ] **Step 5: Коммит**

```bash
git add cabinet/templates/login.html tests/test_login_screens.py
git commit -m "Вход: сказано, что аккаунт заводится сам"
```

---

### Task 4: Подсказка на экране кода — спам и смена адреса

**Файлы:**
- Изменить: `cabinet/templates/login_waiting.html`
- Тест: `tests/test_login_screens.py` (дополнить)

**Интерфейсы:**
- Использует: `mail`, `resend_action` — уже переданы в контекст.

- [ ] **Step 1: Написать падающий тест**

```python
def test_waiting_screen_helps_when_the_letter_is_late(waiting_html: str) -> None:
    assert "Спам" in waiting_html
    assert "/login" in waiting_html  # смена адреса ведёт на форму входа


def test_waiting_screen_keeps_the_refusal_and_the_hint_apart() -> None:
    """Подсказка про спам не подменяет причину отказа.

    Неверный код и «письма нет» — разные вещи: если смешать их в один текст,
    человек с опечаткой пойдёт искать спам вместо того, чтобы перечитать код.
    """
    html = render_login_waiting(mail="u@example.com", error="Код не подошёл")
    assert "Код не подошёл" in html
    assert "Спам" in html
```

- [ ] **Step 2: Прогнать — падает**

Запуск: `python -m pytest tests/test_login_screens.py -k letter_is_late -v`
Ожидание: FAIL.

- [ ] **Step 3: Добавить подсказку**

Под кнопкой повтора: «Письма нет? Проверьте папку «Спам» — и правильность адреса:
<a href="/login">сменить адрес</a>». На форме входа адрес останется в поле
(`value="{{ mail or '' }}"` там уже есть).

- [ ] **Step 4: Прогнать — проходит; весь набор**

Запуск: `python -m pytest tests/test_login_screens.py -v` → PASS; `python -m pytest -q`.

- [ ] **Step 5: Коммит**

```bash
git add cabinet/templates/login_waiting.html tests/test_login_screens.py
git commit -m "Экран кода: подсказка про спам и смену адреса"
```

---

## Проверка после всех задач

- [ ] `python -m pytest -q` — весь набор зелёный.
- [ ] `ruff check .` и `ruff format --check .` — чисто.
- [ ] Живой прогон на стенде: покупка показывает шаги; регион — дату; вход и
      экран кода — подсказки. Затем выкладка кабинета и проверка на проде.
