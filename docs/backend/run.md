# Запуск бэкенда ИИкузницы

Нужны Python 3.12 и PostgreSQL 16. Все три режима используют один DATABASE_URL, один каталог сотрудников II_CONFIG и одну конфигурацию интеграций. Из корня репозитория:

```sh
python -m venv .ii-local/venv
. .ii-local/venv/bin/activate
pip install -r requirements-backend.txt
docker run -d --name ii-postgres -p 127.0.0.1:5448:5432 \
  -e POSTGRES_DB=iikuznica -e POSTGRES_PASSWORD=dev_only postgres:16-alpine
export DATABASE_URL=postgresql://postgres:dev_only@127.0.0.1:5448/iikuznica
export II_CONFIG="$PWD/.ii-local/identities.json"
python - <<'PY'
from hashlib import sha256
from pathlib import Path
import json, os, secrets
p = Path(os.environ["II_CONFIG"])
users = []
for id, roles in (("employee1", ["employee"]), ("employee2", ["employee"]), ("quality", ["quality"]), ("ops", ["ops"])):
    token = secrets.token_urlsafe(32)
    users.append(dict(id=id, roles=roles, email=f"{id}@example.test", bearer_sha256=sha256(token.encode()).hexdigest()))
    print(id, token)
with p.open("x") as out:
    json.dump(dict(identities=users, engine="fixture-alpha", integrations={"batch": []}), out)
PY
python -m backend migrate
```

Запустите в отдельных процессах с теми же переменными окружения.

```sh
python -m backend api
python -m backend interactive
python -m backend background
```

Swagger UI открывается на `http://127.0.0.1:8000/docs`. В Authorize введите выданный токен. Для новой ленты клиент создаёт UUID. Начните с POST /v1/conversations/{id}/messages с телом `{"client_message_id":"example-1","text":"Сколько дней рассматривают заявку?"}`. Для сценария без ответа начните с пустого корпуса. Ход вернёт предложение отправки. Передайте его ID и версию в confirm. Фоновый процесс отправит письмо в локальный имитатор. Получите Message-ID и от имени учебного ящика ответьте в цепочку:

```sh
python -m backend mail-list
python -m backend mail-reply '<Message-ID из списка>' 'Заявку рассматривают в течение 2 дней.'
```

У исходного сотрудника ответ появляется в истории и SSE. С токеном второго сотрудника задайте тот же вопрос в новой ленте. Ссылка в ответе открывает опубликованный фрагмент. Имитатор хранит письма между перезапусками в `.ii-local/mail.sqlite`. II_LOCAL_MAILBOX задаёт другой путь. Для тестов проверяемых ссылок и переключения движков команда `python -m backend seed-fixtures --version 1` загружает синтетические документы. Второй движок выбирается значением `engine: fixture-beta` в конфигурации.

## Подключения

- **Идентификация.** Локальный каталог хранит SHA-256 непрозрачных случайных bearer-токенов и роли employee, quality, ops. Поле dion_id связывает сотрудника с UUID пользователя корпоративного DION. Параметр enabled отключает доступ. После изменения файла перезапустите API и воркеры. Сырые письма и служебные операции доступны по разным ролям. Через обратный прокси используйте HTTPS.
- **Почта.** MAIL_MODE принимает simulator, network или disabled. Для network задаются MAIL_SENDER, MAIL_RECIPIENT, MAIL_REPLY_TO, MAIL_TRUSTED_SENDERS, MAIL_AUTHSERV_ID, MAIL_SMTP_HOST/PORT/USER/PASSWORD, MAIL_IMAP_HOST/PORT/USER/PASSWORD и MAIL_MAILBOX. SMTP использует STARTTLS или MAIL_SMTP_SSL=true, IMAP использует TLS. Входной шлюз должен удалять чужие Authentication-Results со своим идентификатором и подтверждать конкретный ящик. Включение MAIL_TRUST_GATEWAY_MAILBOX_POLICY предполагает, что шлюз проверяет право отправителя выступать от имени этого адреса. Для корпоративной проверки нужны согласованные ящики бота, сотрудника и качества.
- **Документы.** В II_CONFIG поле integrations.confluence содержит base_url, spaces и token либо username/password. Передайте в spaces доступные всем сотрудникам пространства и проверьте ограничения страниц. В integrations.batch передаётся массив страниц с external_id, version, title, storage_html, опциональными space_key и acl_actor_ids. Через POST /v1/ops/documents/{source_key}/sync запустите batch или confluence и по возвращённому run_id прочитайте состояние. Текст снимка и контрольная сумма хранятся с остальными данными в PostgreSQL.
- **DION.** integrations.dion содержит base_url, email и password технического бота. public_base_url задаёт адрес для ссылок. Фоновый воркер обслуживает long polling и исходящие попытки. Контрактные тесты используют имитатор. На корпоративном стенде проверьте положительную и асинхронно отклонённую доставку, истечение токена, смещение курсора и соответствие промежуточного ID окончательному.
- **Модель.** Встроенные fixture-alpha и fixture-beta реализуют AnswerEngine v1. При подключении поискового ядра оно передаёт каждый физический сетевой вызов, включая судью и повтор, через request.attempts.call с ролью вызова. Обвязка связывает попытку с ходом и trace_id. ModelAttemptResult и ModelAttemptFailure передают фактический маршрут, количество токенов и доступную стоимость. Журнал и агрегаты доступны по /v1/ops/model-calls и /v1/ops/model-costs.

## Проверка и восстановление

```sh
pip install pytest
export TEST_DATABASE_URL="$DATABASE_URL"
python -m pytest tests/acceptance/test_00_quality_loop.py
python -m pytest -s tests/backend tests/acceptance
python -m backend.contracts.export --check
npm ci --prefix contracts
npm run check --prefix contracts
```

Тест восстановления требует pg_dump и pg_restore версии сервера или новее и права создать временную БД. PostgreSQL-тесты создают собственные схемы. Режим `--once` у каждого воркера служит для воспроизводимого прогона одной итерации. Для штатной резервной копии используйте `pg_dump -Fc` и `pg_restore --exit-on-error` в новую БД. Тест проверяет связь истории с фактически восстановленным текстом и его хешем. Живой пробный вызов OpenRouter включается через II_LIVE_MODEL=1 и OPENROUTER_API_KEY. Он использует одну короткую синтетическую реплику на фиксированном маршруте. Проверка локальных TLS-ящиков включается через II_LOOPBACK_MAIL=1 при запущенном стенде GreenMail.
