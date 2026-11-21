# Разбор логики `itsm-mock` и реализация общего контракта изменения инцидентов из дашборда

Документ фиксирует анализ текущей реализации модуля `itsm-mock` в репозитории `Hardcode17/mts_dashboard_backend`, новое продуктовое решение о **возможности изменения инцидентов прямо из нашего дашборда** (назначение ответственного, смена статуса, смена назначенной группы) и спецификацию доработки `itsm-mock` и основного бэкенда (`KAN-57`, `KAN-58`).

---

## 1. Новое архитектурное решение: изменение инцидентов из дашборда

В ранних версиях документации рассматривался вариант, где дашборд работает только на чтение снимков ITSM, а любое изменение официального назначения, статуса или группы выполняется во внешнем интерфейсе ITSM по ссылке «Детальнее» (`KAN-14`, `KAN-47`).

**Принято обновлённое решение:**
1. **Из нашего дашборда координатор может изменять инциденты во внешней ITSM**:
   - назначать и менять **ответственного исполнителя** (выбирая сотрудника из справочника пользователей ITSM);
   - изменять **статус инцидента** (`Assigned`, `WorkInProgress`, `Resolved`, `Closed`, `Research`);
   - изменять **назначенную группу (команду)** (`assignedGroupName`).
2. **Единый источник истины сохраняется в ITSM (`itsm-mock`)**:
   - Фронтенд (`Hardcode17/mts_dashboard_front`) не обращается к ITSM напрямую из браузера и не хранит локально несинхронизированные статусы или назначения.
   - Координатор вызывает защищённый человеческим Bearer JWT (Keycloak, `KAN-16`/`KAN-22`) API нашего бэкенда (`Hardcode17/mts_dashboard_backend`).
   - Бэкенд обращается к `itsm-mock` по **Общему контракту** (`PATCH /itsm_incident/{sys_id}`, `GET /user`, `GET /itsm_incident`, `GET /itsm_incident/{sys_id}`), применяет подтверждённый ответ к локальной базе PostgreSQL, пишет запись в журнал изменений с указанием автора-координатора и при передаче инцидента внешнему исполнителю или группе решения автоматически завершает открытую внутреннюю задачу координатора (`KAN-32`).

---

## 2. Анализ текущей логики `itsm-mock` в `Hardcode17/mts_dashboard_backend`

Модуль `itsm-mock` расположен в `Hardcode17/mts_dashboard_backend/itsm-mock` (пакет `ru.hardcode.itsmmock`) и представляет собой отдельное Spring Boot приложение.

### 2.1. Текущие компоненты `itsm-mock`

| Класс | Текущая ответственность и поведение |
| --- | --- |
| `MockApplication` | Точка входа Spring Boot (`@EnableConfigurationProperties(MockSettings.class)`, `@EnableScheduling`), регистрирует `Clock.systemUTC()`. |
| `MockSettings` | Настройки `itsm.mock.*`: `targetUrl` (`http://backend:8080/api/integrations/itsm/batches`), `tokenUrl`, `clientId`, `clientSecret`, `sourceId` (`demo-itsm`), `seed` (`53`), `batchSize` (`1..500`, по умолчанию `12`), `stateFile` (`/var/lib/itsm-mock/snapshot.json`), `fixtureFile`, `timeout`, `retryDelay`, `maxAttempts`. |
| `IncidentGenerator` | Детерминированный генератор по `seed` и `batchSize`. Создаёт записи инцидентов из 46 полей в формате `character_problems.json` (`sysId` вида `000700000000`, `id` вида `INC000700000000`). На каждом следующем тике (`next(previous, version, now)`), кроме каждого третьего (`version % 3 == 0`), выбирает один случайный инцидент и циклически переключает его статус (`Assigned → WorkInProgress → Resolved → Closed → Research`). |
| `SnapshotStore` | Атомарно читает и сохраняет состояние мока в JSON-файл `stateFile` (`Files.createTempFile` + `FileChannel.force` + `ATOMIC_MOVE`). Хранит `State(sourceId, deliveredVersion, rows, pending, rejection)`. |
| `SnapshotSender` | Управляет жизненным циклом формирования и отправки снимков. При вызове `emit(manual)` создаёт `pending`-пакет с новой монотонной версией `deliveredVersion + 1` и случайным UUID `batchId`, отправляет его через `BatchTransport` с ретраями и при `202 Accepted` фиксирует `deliveredVersion`. |
| `SnapshotSchedule` | По расписанию (`@Scheduled` с интервалом `itsm.mock.interval`, по умолчанию `PT30S`) вызывает `sender.emit(false)`. |
| `BatchTransport` | Получает OAuth2 access token (`grant_type=client_credentials` на `tokenUrl`) и отправляет `POST` на `targetUrl` с заголовками `Authorization: Bearer <token>`, `Idempotency-Key: <batchId>`, `X-Source-Id: <sourceId>`, `X-Source-Version: <version>`. Считает успехом только HTTP `202`, ретраит `429` и `5xx`, на остальных кодах останавливает периодическую отправку. |
| `IncidentDirectory` | Read-only обёртка над `sender.rows()`. Ищет инцидент только по строковому `id` (`INC...`) и возвращает усечённую проекцию `IncidentView(id, stateEnum, priority, assignedGroupName, lastAssignmentAt)` из 5 полей. |
| `MockController` | Служебный контроллер под `/api/mock`: `GET /api/mock/incidents/{incidentId}`, `POST /api/mock/emit`, `GET /api/mock/status`. |

### 2.2. Что сейчас не позволяет реализовать двусторонний контракт без доработки

1. **Отсутствуют эндпоинты контракта ITSM:**
   - нет `GET /itsm_incident` (список всех инцидентов);
   - нет `GET /itsm_incident/{sys_id}` (поиск по `sysId` и возврат полного объекта инцидента; существующий `GET /api/mock/incidents/{incidentId}` ищет только по `id` и возвращает всего 5 полей);
   - нет `GET /user` (справочник пользователей ITSM вообще не выделен в отдельную сущность/каталог);
   - нет `PATCH /itsm_incident/{sys_id}` (нет возможности изменять ответственного, статус или группу).
2. **Пустое состояние `state.rows()` до первого `emit()`:**
   - В конструкторе `SnapshotSender` при отсутствии файла `stateFile` на диске `store.read(...)` возвращает `State` с пустым списком `rows = List.of()`. Заполнение `rows` происходит только внутри `createPending()` при первом вызове `emit()`. Если клиент обратится к `GET /itsm_incident`, `GET /itsm_incident/{sys_id}` или `GET /user` сразу после старта мока до первого тика таймера, список будет пуст.
3. **Перезатирание состояния при работе с `fixtureFile`:**
   - В `SnapshotSender.createPending()` написано:
     ```java
     var rows = settings.fixtureFile() == null
             ? generator.next(state.rows(), version, clock.instant())
             : readFixture();
     ```
     Если задан `fixtureFile` (например, `character_problems.json`), на каждом 30-секундном тике `readFixture()` заново читает неизменный файл с диска и **полностью затирает** `state.rows()`, уничтожая любые изменения, которые были бы внесены через `PATCH /itsm_incident/{sys_id}`.
4. **Несогласованность `pending`-пакета при мутации:**
   - Если в `SnapshotStore` уже сформирован `pending`-пакет (например, во время паузы или ретрая) и в этот момент приходит `PATCH /itsm_incident/{sys_id}`, отправка старого `pending.body()` приведёт к доставке в бэкенд устаревшего состояния до `PATCH`.

---

## 3. Спецификация «5. Общий контракт» для `itsm-mock`

| Действие | Метод | Endpoint | Назначение |
| --- | --- | --- | --- |
| Получить список инцидентов | `GET` | `/itsm_incident` | Возвращает полный список текущих инцидентов в состоянии ITSM (`rows`) |
| Получить инцидент по ID | `GET` | `/itsm_incident/{sys_id}` | Возвращает полную запись инцидента по его системному идентификатору `sys_id` (`sysId`) |
| Получить список пользователей | `GET` | `/user` | Возвращает справочник пользователей ITSM, доступных для назначения ответственными |
| Назначить ответственного | `PATCH` | `/itsm_incident/{sys_id}` | Изменяет ответственного сотрудника (`assignedEmployeeLogin`, `assignedEmployee`) |
| Изменить статус | `PATCH` | `/itsm_incident/{sys_id}` | Изменяет статус (`stateEnum`, `stateId`, `state`) с синхронизацией `completionDate` и `solution` |
| Изменить группу | `PATCH` | `/itsm_incident/{sys_id}` | Изменяет назначенную группу (`assignedGroupName`) и время назначения `lastAssignmentToTeamDateTime` |

---

### 3.1. `GET /itsm_incident` — Получить список инцидентов

- **Ответ `200 OK` (`application/json`):** JSON-массив всех текущих инцидентов из состояния мока. Каждый объект содержит полный набор атрибутов выгрузки (46 полей, включая `sysId`, `id`, `stateEnum`, `stateId`, `state`, `priorityId`, `priority`, `assignedGroupName`, `assignedEmployee`, `assignedEmployeeLogin`, `submitDate`, `deadline`, `completionDate`, `lastAssignmentToTeamDateTime`, `solution` и др.).
- Если мок только что запустился с чистым томом (`state.rows().isEmpty()`), начальный набор инцидентов инициализируется при старте из `fixtureFile` либо через `IncidentGenerator`, поэтому `GET /itsm_incident` всегда возвращает готовый актуальный список.

### 3.2. `GET /itsm_incident/{sys_id}` — Получить инцидент по ID

- **Параметр пути:** `sys_id` — строковый системный идентификатор инцидента (поле `sysId` в JSON, например `"000700000000"` в синтетическом генераторе или `"179413934903718013"` в `character_problems.json`; допускается fallback-сопоставление по `id` вида `INC...`).
- **Ответ `200 OK` (`application/json`):** полный JSON-объект найденного инцидента.
- **Ответ `404 Not Found` (`application/json`):** если инцидент с указанным `sys_id` отсутствует:
  ```json
  {
    "code": "ITSM_INCIDENT_NOT_FOUND",
    "message": "Incident with sys_id 999999999999 not found"
  }
  ```

### 3.3. `GET /user` — Получить список пользователей

- **Ответ `200 OK` (`application/json`):** JSON-массив пользователей ITSM (внешних сотрудников), которых координатор может назначить ответственными за инцидент.
- **Откуда берётся список пользователей в `itsm-mock`:**
  1. Базовый пул сотрудников по командам стенда (`demo0`..`demo3` и дополнительные дежурные специалисты для каждой из групп `TEAMS`).
  2. Все уникальные сотрудники из текущих инцидентов `state.rows()` (по полям `assignedEmployeeLogin` / `assignedEmployee` и `initiatorLogin` / `initiatorName`, что обеспечивает полную совместимость с `character_problems.json`, например `unclefedor` — `"Дядя Фёдор (unclefedor)"`, `piglet` — `"Пятачок (piglet)"` и т.д.).
- **Формат элемента списка `GET /user`:**
  ```json
  [
    {
      "sysId": "user-demo0",
      "login": "demo0",
      "name": "Дежурный 1 (demo0)",
      "groupName": "КЦ ИТ Координаторы Массовых Инцидентов"
    },
    {
      "sysId": "user-unclefedor",
      "login": "unclefedor",
      "name": "Дядя Фёдор (unclefedor)",
      "groupName": "Простоквашино ТехПоддержка"
    }
  ]
  ```

### 3.4. `PATCH /itsm_incident/{sys_id}` — Назначить ответственного, изменить статус, изменить группу

Маршрут `PATCH /itsm_incident/{sys_id}` принимает `application/json` и позволяет выполнить одно, два или все три действия над инцидентом за один запрос.

#### Вариант А: Назначить ответственного
Тело запроса может передавать логин/идентификатор пользователя из справочника `GET /user` (например, `assignedEmployeeLogin` или `userId` / `sysId`, а также опционально `assignedEmployee`):
```json
{
  "assignedEmployeeLogin": "demo1"
}
```
- Мок проверяет наличие пользователя в справочнике `GET /user`, устанавливает в записи инцидента:
  - `assignedEmployeeLogin`: `"demo1"`
  - `assignedEmployee`: `"Дежурный 2 (demo1)"`
- Если передано `"assignedEmployeeLogin": null` (или `"assignedEmployee": null`), ответственный снимается (`null` в обоих полях).
- Если передан неизвестный пользователь — возвращается `400 Bad Request` (`ITSM_USER_NOT_FOUND` / `ITSM_INVALID_PATCH`).

#### Вариант Б: Изменить статус
Тело запроса передаёт новый статус (`stateEnum`, либо `stateId` / `state`) и при необходимости текст решения `solution`:
```json
{
  "stateEnum": "WorkInProgress"
}
```
или при переводе в завершённое состояние / исследование:
```json
{
  "stateEnum": "Resolved",
  "solution": "Перезапущен пул соединений платежного шлюза."
}
```
- **Синхронизация полей статуса** в записи инцидента:
  | `stateEnum` | `stateId` | `state` | Категория |
  | --- | ---: | --- | --- |
  | `Assigned` | `0` | `Назначен` | Активный |
  | `WorkInProgress` | `2` | `Выполняется` | Активный |
  | `Resolved` | `7` | `Завершен` | Завершённый |
  | `Closed` | `10` | `Закрыт` | Завершённый (терминальный) |
  | `Research` | `20` | `Исследование ситуации` | Завершённый (на исследовании) |
- **Соблюдение инвариантов валидатора `KAN-21` (`ItsmIncidentSnapshotParser` и `ck_itsm_incident_completion` в `V4__itsm_domain_baseline.sql`):**
  - Для `Assigned` и `WorkInProgress`: `completionDate` устанавливается в `null`, `solution` очищается (`""` / `null`).
  - Для `Resolved`, `Closed` и `Research`: если `completionDate` был `null`, он заполняется текущим UTC-временем `clock.instant().truncatedTo(ChronoUnit.MICROS).toString()`; если `solution` не передан и был пустым, подставляется непустое решение (`"Восстановление подтверждено на стенде."`).
  - Переход из терминального статуса `Closed` обратно в другие статусы отклоняется с `409 Conflict` (`ITSM_INVALID_STATE_TRANSITION`). Неизвестный статус отклоняется с `400 Bad Request`.

#### Вариант В: Изменить группу
Тело запроса передаёт новую группу (`assignedGroupName`):
```json
{
  "assignedGroupName": "Дежурные платёжных сервисов"
}
```
- Мок проверяет, что `assignedGroupName` — непустая строка.
- Обновляет `assignedGroupName` в записи инцидента.
- Если группа изменилась, обновляет `lastAssignmentToTeamDateTime` на текущее время UTC (`clock.instant().truncatedTo(ChronoUnit.MICROS).toString()`).

#### Комбинированный `PATCH`
Допускается передача нескольких полей одновременно (например, координатор одновременно назначает группу, ответственного и переводит статус в `WorkInProgress`):
```json
{
  "assignedGroupName": "Дежурные платёжных сервисов",
  "assignedEmployeeLogin": "demo1",
  "stateEnum": "WorkInProgress"
}
```

- **Ответ `200 OK` (`application/json`):** полный обновлённый JSON-объект инцидента.
- **Сохранение и доставка:** изменённый массив `rows` немедленно сохраняется в `SnapshotStore`. Следующий плановый (каждые 30 с) или ручной (`POST /api/mock/emit`) снимок отправляет в `POST /api/integrations/itsm/batches` уже обновлённое состояние инцидента.

---

## 4. Сквозной сценарий: от действия в дашборде до обновления ITSM и БД

```text
Координатор (UI)
   │  1. Открывает карточку INC, запрашивает список исполнителей
   ▼
Dashboard Backend (GET /api/dashboard/users → GET /user в itsm-mock)
   │  2. Координатор меняет ответственного / статус / группу в карточке
   ▼
Dashboard Backend (PATCH /api/dashboard/incidents/{incidentId})
   │  3. Находит sys_id инцидента в itsm_incident, вызывает PATCH /itsm_incident/{sys_id}
   ▼
ITSM Mock (PATCH /itsm_incident/{sys_id})
   │  4. Валидирует, обновляет запись в SnapshotStore, возвращает 200 OK с обновлённым INC
   ▼
Dashboard Backend
   │  5. В транзакции обновляет itsm_incident, справочники, активное назначение и журнал itsm_incident_change
   │     (при передаче внешнему исполнителю/команде решения закрывает внутреннюю задачу координатора KAN-32)
   │  6. Возвращает обновлённую карточку DashboardIncidentCardResponse (200 OK) в UI
   ▼
ITSM Mock (плановый тик каждые 30 с: POST /api/integrations/itsm/batches)
   │  7. Присылает полный снимок с тем же изменённым INC; сверка KAN-55 видит совпадение и не плодит дублей
```

---

## 5. Задачи в Jira

Реализация контракта зафиксирована в проекте `KAN` с максимальным приоритетом (`Highest`) и назначена на **Никиту Подкопаева**:

- **[KAN-57](https://sokolenkonikita12ru.atlassian.net/browse/KAN-57)** — *ITSM-мок: реализовать общий REST-контракт чтения и изменения инцидентов (GET/PATCH /itsm_incident, GET /user)* (`priority: Highest`, `repo:backend`).
- **[KAN-58](https://sokolenkonikita12ru.atlassian.net/browse/KAN-58)** — *Backend: изменение инцидентов из дашборда (ответственный, статус, группа) и интеграция с контрактом ITSM (/itsm_incident, /user)* (`priority: Highest`, `repo:backend`).
