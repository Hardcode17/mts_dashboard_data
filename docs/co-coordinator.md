# Co‑Coordinator — подбор группы решения

KAN-74 проверяет, помогает ли история завершённых инцидентов подобрать группу ITSM для нового случая. Прототип запускается из `prototypes/routing-advisor/`. В комплекте синтетические русскоязычные случаи и стендовый справочник владельцев сервисов.

## Запуск стенда

Понадобятся Python 3.12, Docker Compose и OpenSSL. Из корня репозитория:

```sh
cd prototypes/routing-advisor
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export ROUTING_DSN="postgresql://routing_advisor:$POSTGRES_PASSWORD@127.0.0.1:5447/routing_advisor"
docker compose up -d --wait

python advisor.py seed
python advisor.py show --id INC-742001
python advisor.py evaluate
python -m unittest -v
```

При повторном запуске сохраните прежний пароль PostgreSQL для существующего тома. `seed` заменяет историю и справочник в схеме `routing_advisor` одной транзакцией. Используйте отдельную базу прототипа. `show` и `evaluate` работают в транзакциях только на чтение.

Для первого запуска нужен доступ к Hugging Face, чтобы один раз загрузить веса [multilingual-e5-small](https://huggingface.co/intfloat/multilingual-e5-small). Эмбеддинги рассчитываются локально на CPU. Для автономного запуска укажите `ROUTING_MODEL=/путь/к/скачанной/модели` и `HF_HUB_OFFLINE=1`. После смены модели выполните `seed` снова. Прототип сверяет модель запроса с моделью индекса, а `evaluate` сверяет историю и справочник с загруженным набором.

## Локальный HTTP-адаптер

`server.py` — тонкая loopback-обёртка над `advisor.py` для локального стенда с фронтендом. Он не копирует правила: эмбеддинги, `check_seed`, SQL-поиск по pgvector и отбор аналогов/владельцев берутся из `advisor.py`. Сервер слушает только `127.0.0.1:8747` (порт меняется через `ROUTING_PORT`), модель загружается и прогревается один раз при старте. Все соединения с PostgreSQL открываются с `default_transaction_read_only=on` и работают в транзакции `REPEATABLE READ READ ONLY`; запросов на запись в адаптере нет.

Запуск в отдельном терминале (после `seed` из раздела выше):

```sh
cd prototypes/routing-advisor
. .venv/bin/activate

export ROUTING_DSN="postgresql://routing_advisor:$POSTGRES_PASSWORD@127.0.0.1:5447/routing_advisor"
export ROUTING_MODEL="intfloat/multilingual-e5-small"   # или путь к локальной копии
python server.py
```

Успешный старт печатает `Routing advisor API: http://127.0.0.1:8747`. Проверка без браузера:

```sh
curl -fsS http://127.0.0.1:8747/health
curl -fsS http://127.0.0.1:8747/catalogue
```

`GET /catalogue` возвращает только допустимые значения из реальных таблиц `routing_advisor.history` и `routing_advisor.owners`:

```json
{"service": ["Wi-Fi", "..."], "site": ["Ростов-на-Дону", "..."], "infrastructure": ["Офисная сеть", "..."]}
```

`POST /search` принимает JSON с коротким симптомом, выбранными параметрами и текущей группой:

```json
{
  "incident_id": "INC-742001",
  "symptom": "Сотрудники головного офиса в Ростове не проходят авторизацию в Wi-Fi",
  "service": "Wi-Fi",
  "site": "Ростов-на-Дону",
  "infrastructure": ["Офисная сеть"],
  "current_group": "Сетевая поддержка Москвы"
}
```

`service` и `site` можно не указывать, но должен быть хотя бы один из `service` или `infrastructure`. Значения `service`, `site` и `infrastructure` сверяются с `/catalogue`; произвольные фильтры отклоняются с `400`. `current_group` необязательна. Тело — не больше 8 КиБ, `symptom` — до 1000 символов, одно значение — до 200 символов, инфраструктур — до 8, обязательны `Content-Type: application/json` и `Content-Length`. Остальные методы и пути возвращают `404/405`; наружу сервер не выставляется.

Ответ — канонический объект `advisor.py` плюс необязательный счётчик кандидатов:

```json
{
  "incident_id": "INC-742001",
  "analogues": [{"incident_id": "INC-741001", "source_ref": "itsm:INC-741001", "matched_signals": ["..."], "differences": ["Площадка аналога: Таганрог"], "outcome": "...", "resolved_by": "Сетевая поддержка Таганрога", "similarity": 0.8929}],
  "recommendation": {"group": "Сетевая поддержка Ростова-на-Дону", "incident_ids": ["INC-741001"], "reasons": ["..."]},
  "candidate_count": 4
}
```

`recommendation` равен `null`, если владельца нет, владельцев несколько или владелец совпадает с `current_group`; история при этом остаётся в `analogues`.

### Полный воспроизводимый пример с настоящей БД

Пример поднимает отдельную PostgreSQL 17 + pgvector 0.8.1 через Compose, загружает стендовый корпус в реальные таблицы и проверяет оба сценария приёмки. Нужны Docker Compose, Python 3.12 и OpenSSL.

```sh
cd prototypes/routing-advisor
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements.txt

export POSTGRES_PASSWORD="$(openssl rand -hex 24)"
export ROUTING_DSN="postgresql://routing_advisor:$POSTGRES_PASSWORD@127.0.0.1:5447/routing_advisor"
export ROUTING_MODEL="intfloat/multilingual-e5-small"
docker compose up -d --wait
python advisor.py seed
```

Проверяем, что данные лежат в PostgreSQL, а не в памяти процесса:

```sh
docker compose exec db psql -U routing_advisor -d routing_advisor -c \
  "SELECT incident_id, service, site, outcome IS NOT NULL AS has_outcome FROM routing_advisor.history WHERE incident_id IN ('INC-741001','INC-741007');"
docker compose exec db psql -U routing_advisor -d routing_advisor -c \
  "SELECT service, site, owner_group FROM routing_advisor.owners WHERE service = 'Wi-Fi' ORDER BY site;"
```

Запускаем адаптер в отдельном терминале с теми же `ROUTING_DSN` и `ROUTING_MODEL`. Сохраните пароль из первого терминала и повторите его экспорт:

```sh
cd prototypes/routing-advisor
. .venv/bin/activate
export POSTGRES_PASSWORD="<пароль из первого терминала>"
export ROUTING_DSN="postgresql://routing_advisor:$POSTGRES_PASSWORD@127.0.0.1:5447/routing_advisor"
export ROUTING_MODEL="intfloat/multilingual-e5-small"
python server.py
```

Сценарий (а): вход в Wi-Fi в Ростове находит аналог из Таганрога и ростовскую группу из справочника.

```sh
curl -fsS -X POST http://127.0.0.1:8747/search \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "incident_id": "INC-742001",
    "symptom": "Сотрудники головного офиса в Ростове не проходят авторизацию в Wi-Fi",
    "service": "Wi-Fi",
    "site": "Ростов-на-Дону",
    "infrastructure": ["Офисная сеть"],
    "current_group": "Сетевая поддержка Москвы"
  }' | python -m json.tool
```

Ожидаемо: в `analogues` есть `INC-741001` с `resolved_by: "Сетевая поддержка Таганрога"`, а `recommendation.group` равен `"Сетевая поддержка Ростова-на-Дону"`.

Сценарий (б): у телефонии в Ростове-на-Дону два владельца, поэтому история возвращается, а совета нет.

```sh
curl -fsS -X POST http://127.0.0.1:8747/search \
  -H 'Content-Type: application/json' \
  --data-binary '{
    "incident_id": "INC-742003",
    "symptom": "Исходящие городские звонки обрываются",
    "service": "Телефония",
    "site": "Ростов-на-Дону",
    "infrastructure": ["АТС"],
    "current_group": "Сетевая поддержка Москвы"
  }' | python -m json.tool
```

Ожидаемо: `analogues` содержит `INC-741007`, `recommendation` равен `null`. Это правило `advisor.summarize`: при двух владельцах интерфейсу доступна только история.

Для проверки отказа формата:

```sh
curl -sS -i -X POST http://127.0.0.1:8747/search \
  -H 'Content-Type: application/json' \
  --data-binary '{"incident_id":"x","symptom":"x","service":"Wi-Fi","site":"Несуществующая площадка"}'
# HTTP/1.1 400 Bad Request
```

Фронтенд не ходит в PostgreSQL и не обращается к произвольному адресу: браузер делает same-origin запросы к `/routing-advisor/*`, а Vite dev/preview проксирует их на loopback-адаптер. Продуктовый прокси через Keycloak/бэкенд и деплой на Pi в этот стенд не входят.

## Правило поиска и назначения

1. Для запроса и истории берём название и описание симптома. Перед расчётом эмбеддингов убираем URL, email и IPv4. Для рабочего набора нужно отдельно подтвердить качество обезличивания. Решение, группы и разметка не входят во вход модели.
2. [pgvector](https://github.com/pgvector/pgvector) считает косинусное сходство среди `Resolved` и `Closed` по тому же сервису или общей инфраструктуре. Текущая карточка исключается. Берём 10 ближайших, затем учитываем небольшой бонус за совпадение площадки.
3. В ответе остаётся до трёх случаев с известным исходом. Для стенда используем порог 0,86 и окно 0,02 от лучшего случая с известным исходом. Эти параметры проверены на синтетическом наборе и требуют отдельной калибровки на реальной истории. В `evaluate` видны оценки и причины отсева.
4. Рекомендация появляется, когда есть хотя бы один аналог и справочник однозначно указывает другую группу для точной пары «сервис × площадка». При конфликте, неизвестной площадке или совпадении с текущей группой интерфейсу доступны только аналоги.

В примере `INC-742001` история Wi-Fi из Таганрога объясняет похожий сбой. Предлагаемая группа берётся из справочника Ростова-на-Дону. `resolved_by` хранит подтверждённую историческую группу решения, а `first_assigned_group` — первую группу в карточке.

## Формат фоновой интеграции

| Поле | Содержимое |
|---|---|
| `incident_id` | Номер исследуемой карточки |
| `analogues[]` | Номер и ссылка вида `itsm:<номер>`, совпадения, отличия, известный исход, подтверждённая группа решения или `null`, оценка сходства |
| `recommendation` | `null` или объект с одной группой, номерами аналогов и основаниями |

Интеграция может преобразовать `source_ref` в ссылку на карточку ITSM. Поиск KAN-74 читает историю. Координатор отдельно подтверждает действие. API, интерфейс, запись в ITSM и предложение связей между карточками остаются задачами интеграции.

## Сравнение с ключевыми словами

Команда `evaluate` проверяет один и тот же размеченный набор двумя способами. Базовый вариант ищет пересечения слов в том же сервисе. Hit@3 — доля запросов с известным аналогом, где хотя бы один размеченный случай вошёл в первые три результата. Для отрицательных запросов и рекомендаций группы выводятся отдельные значения. Разметка используется при подсчёте результатов.

Стендовый прогон на PostgreSQL 17 и pgvector 0.8.1 дал 8/8 по Hit@3 для обоих методов. Векторный поиск вернул 11 примеров, из которых 9 отмечены как релевантные. Для двух отрицательных запросов аналогов и предложений не было. Совет или его отсутствие совпали с ожидаемыми во всех 10 сценариях. Этот набор проверяет поведение прототипа и пока не показывает преимущества векторного поиска.
