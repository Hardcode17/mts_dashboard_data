# План интеграции фронта и реального бэкенда

Оркестрационный план: выпилить мок-стенд фронта и связать дашборд исключительно с реальным
backend (`Hardcode17/mts_dashboard_backend`). Владелец направления — Артем (KAN-64, KAN-65,
KAN-52). Дата: 2026-11-21.

## 1. Точка отправления

**Backend (mts_dashboard_backend, main `cf4ac23`)** — готов к подключению:

* чтения: `GET /api/dashboard/incidents` (4 группы, ETag/304, cursor, CSV),
  `GET /api/dashboard/incidents/{id}` (карточка + `assignedEmployeeLogin`),
  `GET /api/dashboard/users` (справочник исполнителей ITSM), `GET /api/snapshot/status`;
* изменения: `PATCH /api/dashboard/incidents/{id}` (ответственный/статус/группа) с
  синхронизацией внешней ITSM (`itsm-mock`, контракт `GET/PATCH /itsm_incident`, `GET /user`,
  HTML-страница «Детальнее» в стиле SimpleOne) — KAN-57, KAN-58;
* доступ: Keycloak realm с публичным SPA-клиентом `dashboard-web`
  (Authorization Code + PKCE, audience `dashboard-api`), локальный допуск координатора —
  KAN-22 (`users` по iss/sub/azp);
* модель публикации: `dashboard_publication` (eventSeq/revision) движется и воркером
  снимков, и PATCH-редактированиями; журнал изменений пишет автора.

Осталось на бэке: **KAN-64** — CORS для origin'а фронта + включение человеческой JWT-цепочки
на демо-стенде (сейчас CORS-конфигурации нет вообще — жёсткий блокер №1 для браузера).

**Frontend (mts_dashboard_front)** — целиком на собственном мок-стенде
(`mock/server.ts` + SQLite, обслуживает dev и прод; healthcheck CI стучит в мок;
контракт `/api/stand/*` не существует на бэке; демо-данные `src/demoData.ts` подмешиваются
в клиент). Все обмен изолирован в `src/desk/useDesk.ts` — замена транспорта не трогает UI.

Канонический контракт бэка: `mts_dashboard_backend/docs/dashboard-api.openapi.yaml`
(примеры валидированы контрактор-тестом).

## 2. Потоки работ (параллельные, файлы не пересекаются)

| Поток | Тикет | Репо | Файловая зона (владение) | Зависимости |
|---|---|---|---|---|
| A — логин и api-клиент | KAN-72 | front | `src/api/client.ts`, `src/auth/*`, `.env.example`, hash-роут `/callback` | — |
| B — транспорт useDesk | KAN-68 | front | `src/desk/useDesk.ts`, `src/desk/backend.ts`, `src/desk/context.ts` | интерфейс `apiFetch` из A (писать сразу против контракта ниже) |
| C — вычистка демо-данных | KAN-69 | front | `src/demoData.ts`, `src/App.tsx` (блоки подмешивания), `src/desk/timeline.ts`, `src/incidentHistory.ts`, `src/desk/types.ts` (standNow) | — |
| D — экран расследования | KAN-71 | front | `src/AgentInvestigation.tsx`, `src/agentDemo.ts`, (опц.) `src/ShiftCharts.tsx` | — |
| E — прод/CI без мока | KAN-70 | front | `compose.yaml`, `Dockerfile`, `.github/workflows/deploy.yml`, `mock/start.mjs`, `vite.config.ts`, `package.json`, `.dockerignore` | — |
| F — тесты и e2e | KAN-73 | front | `tests/*` | E (для e2e-стека), B (для контракта) |
| G — CORS + auth бэка | KAN-64 | back | security-цепочки, `application.yaml`, compose | — |
| H — UI редактирования | KAN-12 | front | `src/IncidentView.tsx`, `src/desk/CardWork.tsx` (UI-часть) | B (транспорт PATCH/users) |
| I — сквозная приёмка | KAN-52 | back | IT-сценарии | A–H |

Правила параллельности:

1. Один файл — один поток. Спорные места (`App.tsx`, `types.ts`) меняются только в рамках
   описанных блоков.
2. Каждый поток — своя ветка `feature/KAN-<номер>-<slug>`, работает от свежего `main`.
3. Запускать имплементацию субагентами на модели **GLM-5.3-flash** (opencode-go):
   максимум 7 одновременно (A, B, C, D, E, G стартуют сразу; H — после мержа B; F — после E).
4. Слияние в main — только через зелёные `npm run lint` + `npm run build` (+ тесты для F),
   порядок мержей: A → B → (C, D, E в любом порядке) → H → F.

## 3. Контракт замены `/api/stand/*`

| Было (мок) | Стало (реальный бэк) |
|---|---|
| `GET /api/stand/snapshot?mode=…&since=…` | `GET /api/dashboard/incidents?group=critical&group=coordinator&group=high&group=research&includeCompleted=false` (+ `GET /api/dashboard/incidents/{id}` для деталей) |
| поле `version` | `eventSeq`/`revision` ответа; polling 5с, ETag/304 — пропуск пересборки |
| `POST /api/stand/action` (28 команд) | редактирование: `PATCH /api/dashboard/incidents/{id}`; справочник: `GET /api/dashboard/users`; остальное — локальная заглушка «не поддерживается бэкендом» до KAN-25/26/28/29 |
| `GET /api/stand/shop` | удаляется (поток D) |
| персона из localStorage `mcc-person` | OIDC PKCE → `GET /api/me` (id, login, displayName) |
| `checked`/stale из ответа мока | `GET /api/snapshot/status` → `stale`, `lastSuccessAt` |

Ошибки: JSON `{"code","message"}` (`INCIDENT_NOT_FOUND`, `INCIDENT_PATCH_INVALID`,
`INCIDENT_CONFLICT`, `COORDINATOR_ACCESS_DENIED`, `HUMAN_AUTH_REQUIRED`, `ITSM_OFFLINE`).
Даты UTC ISO-8601 `Z`, `id` — строка `INC…`.

Порты: демо-бэк `100.77.2.49:8081` (или локально `8081`), auth-стенд бэк `127.0.0.1:8091`,
Keycloak `127.0.0.1:8084` (realm из `src/keycloak/realm/dashboard-realm.json`), itsm-mock
`127.0.0.1:8082`.

## 4. Что считается «выпиленными моками» (приёмка KAN-65)

* в прод-образе фронта нет `mock/start.mjs`, SQLite и тома `mcc-stand`; healthcheck CI бьёт
  в `/health` реального бэка;
* `grep -ri "demoData\\|/api/stand\\|agentDemo" src/` — пусто (кроме типов);
* дашборд показывает 4 группы из живого бэка с источником itsm-mock (44 INC из
  `character_problems.json`), `PATCH` из карточки меняет инцидент в ITSM и журнал;
* вход только через Keycloak PKCE, `GET /api/me` отдаёт координатора;
* e2e-тест (поток F) зелёный на стеке фронт+бэк+itsm-mock+Keycloak.

## 5. Не в скопе этого плана (явно)

* каналы уведомлений, комнаты, заметки, таймеры переназначения — backend stage-04
  (KAN-24, KAN-30–36): фронт до них показывает «не поддерживается бэкендом»;
* агентский анализ/телеметрия — KAN-40/52: экран расследования показывает состояние
  «данные ожидаются»;
* events/ack/presets — KAN-25/26/28/29 + KAN-45: подключатся к useDesk поверх того же
  api-клиента после реализации.

## 6. Статус на момент плана

* merge `feature/podkopaev-kan-backend` в backend main (`cf4ac23`) — KAN-22, KAN-57, KAN-58
  реализованы, полный IT-прогон на финальной проверке;
* KAN-14 закрыт, KAN-12 разблокирован и назначен;
* подзадачи KAN-68…KAN-73 созданы, назначены и готовы к раздаче субагентам.
