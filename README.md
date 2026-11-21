# MCC — материалы и контракты дашборда координатора

Здесь собраны исходные материалы кейса МТС и документация команды о работе координатора массовых инцидентов. Начните с обзора сценария смены, затем перейдите к требованиям, исходным данным и контрактам.

## С чего начать

| # | Что открыть | Зачем |
|---|---|---|
| 1 | [Обзор и отличие](docs/index.html#difference) · [путь одного инцидента](docs/index.html#scenario) | Понять проблему смены и сценарий решения |
| 2 | [Функции кейса](docs/requirements.html#core) · [сценарии приёмки](docs/requirements.html#acceptance) | Сверить требования и проверяемое поведение |
| 3 | [Выгрузка на 44 INC](Предоставленные%20материалы/character_problems.json) · [разбор полей](docs/json_data_analysis/character_problems_разбор.md) | Увидеть исходные данные и их ограничения |
| 4 | [OpenAPI 3.1.0](docs/openapi.yaml) · [общий контракт ITSM](docs/itsm-mock-contract.md) · [модель БД v5](docs/database/mts_database_v5_documentation.md) | Проверить контракты и устройство хранения |

В исходном наборе 44 уникальных инцидента. В очередях исходного снимка 0 критических, 2 на координаторах, 1 высокого приоритета и 5 в исследовании. Для проверки сценария критического инцидента используйте копию снимка по [сценариям приёмки](docs/requirements.html#acceptance).

## Целевой поток данных

```text
Исходный снимок ITSM / JSON
        │ импорт, проверка и сравнение версий
        ▼
Backend: текущие INC, журнал, четыре группы, SLA и события
        │ REST API
        ▼
Экран координатора: очередь → карточка → подтверждение или действие
        │
        ├─ ITSM: ответственный, статус, назначенная группа
        └─ контекст: встреча, телеметрия, исследование
```

Четыре группы пересекаются: один INC может одновременно находиться в нескольких. Правила групп, критическое оповещение и изменения в ITSM описаны в [бизнес-ТЗ](docs/requirements.html#core) и [общем контракте](docs/itsm-mock-contract.md).

## Документация и материалы

| Что нужно | Где | Тип |
|---|---|---|
| Исходные материалы | [презентация МТС](Предоставленные%20материалы/МТС.pdf) · [выгрузка INC](Предоставленные%20материалы/character_problems.json) · [словарь атрибутов](Предоставленные%20материалы/incidents_attributes.json) | Источники кейса; не переписываются |
| Требования и решения | [бизнес-ТЗ](docs/requirements.html) · [разбор 58 предложений](docs/proposals.html) · [поддержка решений](docs/decision-support.html) · [Co‑Coordinator: похожие случаи и связи](docs/co-coordinator.md) | Производная документация команды |
| API и контракты | [OpenAPI 3.1.0](docs/openapi.yaml) · [общий контракт ITSM](docs/itsm-mock-contract.md) · [связи инцидентов](docs/incident-relationships.md) · [MCP v1: схемы и примеры](docs/mcp/MCP.md) | Контракты; MCP v1 подготовлен для ревью |
| Данные и модель | [разбор выгрузки](docs/json_data_analysis/character_problems_разбор.md) · [классификация](docs/incident-classification.md) · [БД v5](docs/database/mts_database_v5_documentation.md) · [DBML](docs/database/mts_incidents_v5.dbml) | Производная документация |
| Планы и контекст | [frontend](docs/frontend-plan.md) · [интеграция](docs/integration-plan.md) · [мониторинг](docs/monitoring-mode.md) · [три дня](docs/three-day-plan.md) · [эксплуатация](docs/mts-operations-and-integrations.md) | Планы и рабочие заметки |
| Открытые вопросы | [вопросы](docs/Вопросы.md) · [идеи](docs/ideas/ideas.md) | Материалы обсуждения |

`Предоставленные материалы/` — исходные файлы кейса. `docs/` — производная документация: рабочая редакция команды, а не официальная публикация организаторов. Она может уточняться и не заменяет проверку работающего стенда.

## Предпросмотр и проверка

**Предпросмотр документации.** Из корня репозитория:

```bash
python3 -m http.server 8000 --bind 127.0.0.1
```

Откройте <http://127.0.0.1:8000/docs/>. Чтобы прочитать обзор без локального сервера, откройте [`docs/index.html`](docs/index.html) напрямую в браузере.

**Проверка исходных данных.**

```bash
python3 - <<'PY'
import json
from pathlib import Path

rows = json.loads(Path("Предоставленные материалы/character_problems.json").read_text(encoding="utf-8"))
attrs = json.loads(Path("Предоставленные материалы/incidents_attributes.json").read_text(encoding="utf-8"))
active = {"Assigned", "WorkInProgress"}

print(f"INC: {len(rows)}, unique: {len({r['id'] for r in rows})}, attrs: {len(attrs)}")
print("critical:", sum(r["stateEnum"] in active and r["priorityId"] == 4 for r in rows))
print("coordinators:", sum(r["stateEnum"] in active | {"Research"} and "Координаторы" in (r.get("assignedGroupName") or "") for r in rows))
print("high:", sum(r["stateEnum"] in active and r["priorityId"] == 3 for r in rows))
print("research:", sum(r["stateEnum"] == "Research" for r in rows))
PY
```

Ожидаемый результат:

```text
INC: 44, unique: 44, attrs: 30
critical: 0
coordinators: 2
high: 1
research: 5
```

## Статус контрактов

[OpenAPI в этом репозитории](docs/openapi.yaml) объединяет реализованные и целевые операции. Реализованные маршруты и команды запуска описаны в репозитории бэкенда.

## Обновление стенда

После обновления `main` workflow из этого репозитория вызывает **Deploy backend**
в `Hardcode17/mts_dashboard_backend`. Сборка и доставка выгрузки выполняются
там же.

Добавьте `STAND_DEPLOY_TOKEN` в
[Actions Secrets этого репозитория](https://github.com/Hardcode17/mts_dashboard_data/settings/secrets/actions).
Это [fine-grained token](https://github.com/settings/personal-access-tokens/new)
владельца ресурсов `Hardcode17` с доступом только к `mts_dashboard_backend`
и правом **Repository permissions → Actions → Read and write**. Токен со
статусом **Pending** должен одобрить владелец организации. Если секрет ещё
не настроен, запустите **Deploy backend** вручную на ветке `main`.

## Репозитории проекта

| Репозиторий | Роль |
|---|---|
| [mts_dashboard_front](https://github.com/Hardcode17/mts_dashboard_front) | Интерфейс координатора и локальный демостенд |
| [mts_dashboard_backend](https://github.com/Hardcode17/mts_dashboard_backend) | API, доступ и приём снимков ITSM |
| [mts_dashboard_data](https://github.com/Hardcode17/mts_dashboard_data) | Материалы кейса, требования и контракты |
