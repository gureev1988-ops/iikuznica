# Контракт API v1

[Схема OpenAPI](openapi.v1.json) описывает HTTP-методы, параметры и ответы. [Объявления TypeScript](openapi.v1.d.ts) содержат типы запросов, ответов и событий для клиентов.

## Поток событий диалога

Клиент подписывается на `GET /v1/conversations/{conversation_id}/events`. В обычном событии SSE поле `data` содержит JSON типа `EventView`. При переподключении передайте в заголовке `Last-Event-ID` идентификатор последнего полученного события этого диалога.

Если сервер вернул HTTP 410 или прислал событие `reset` с данными типа `EventCursorExpired`, запросите историю через `GET /v1/conversations/{conversation_id}/messages`. Возобновите подписку с параметром `after`, равным `cursor` из ответа с историей.

## Обновление схемы и типов

После изменения публичных маршрутов FastAPI или их моделей выполните команды из корня репозитория, активировав [Python-окружение проекта](../CONTRIBUTING.md#бэкенд-и-api).

```sh
python -m backend.contracts.export
npm ci --prefix contracts
npm run generate --prefix contracts
```

Команды ниже сверяют файлы с приложением и проверяют объявления компилятором TypeScript.

```sh
python -m backend.contracts.export --check
npm run check --prefix contracts
```

## Интерфейс AnswerEngine v1

В `AnswerRequest` движок получает снимок разрешённых фрагментов и регистратор обращений к модели. Каждый запрос провайдеру, включая повторы и вызовы модели для оценки ответа, выполняйте через `request.attempts.call(invoke, role=..., model=..., route=...)`. Связанные вызовы можно объединить общим `trace_id`.

Верните из `invoke` объект `ModelAttemptResult` с фактическими моделью, маршрутом, числом токенов и стоимостью, которые сообщил провайдер. При технической ошибке передайте эти данные в `ModelAttemptFailure`. Неизвестная стоимость остаётся `None`. Синтетические движки работают в памяти без обращений к провайдеру. [Интерфейс и типы движка](../backend/contracts/engine.py)
