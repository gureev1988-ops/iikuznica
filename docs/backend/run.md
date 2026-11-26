# Запуск бэкенда ИИкузницы

## Демонстрация на хакатоне

Адаптеры Confluence, DION и корпоративной почты подготовлены. В профиле `hackathon` приложение читает регламенты из локального корпуса, сохраняет переписку в почтовом имитаторе и показывает ответы в личной ленте HTTP/SSE. Сетевой бот DION выключен. Движок отвечает по синтетическим сценариям. Для демонстрации нужны Python 3.12 и отдельная локальная база PostgreSQL 16 для учебных данных.

Регламенты демо доступны на чтение всем сотрудникам. Управляет разъяснениями один администратор службы качества. Корпоративному адресу `quality@softline.ru` в имитаторе соответствует `quality@softline.test`. Исходную переписку видит служба качества, а личную ленту — её владелец.

Из корня репозитория подготовьте окружение и каталог участников.

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
for id, roles in (("employee1", ["employee"]), ("employee2", ["employee"]), ("quality", ["employee", "quality"]), ("ops", ["ops"])):
    token = secrets.token_urlsafe(32)
    email = "quality@softline.test" if id == "quality" else f"{id}@example.test"
    users.append(dict(id=id, roles=roles, email=email, bearer_sha256=sha256(token.encode()).hexdigest()))
    print(id, token)
with p.open("x") as out:
    json.dump(dict(profile="hackathon", identities=users, engine="fixture-alpha", integrations={"batch": []}), out)
PY
python -m backend migrate
```

Запустите в отдельных процессах с теми же переменными окружения.

```sh
python -m backend api
python -m backend interactive
python -m backend background
```

На пустом корпусе отправьте вопрос от первого сотрудника. В примере команды обращаются только к API на localhost. Введите токен `employee1`, напечатанный при подготовке каталога.

```sh
read -rs -p 'Токен employee1: ' employee_token
chat_id=$(python -c 'import uuid; print(uuid.uuid4())')
api=http://127.0.0.1:8000/v1
curl -fsS -H "Authorization: Bearer $employee_token" -H 'Content-Type: application/json' \
  -d '{"client_message_id":"example-1","text":"Сколько дней рассматривают заявку?"}' \
  "$api/conversations/$chat_id/messages"
```

После обработки прочитайте снимок ленты. В `turns[].offers[]` найдите открытое предложение и его версию. Подтвердите отправку от имени того же сотрудника.

```sh
curl -fsS -H "Authorization: Bearer $employee_token" "$api/conversations/$chat_id/messages"
read -r -p 'ID предложения: ' offer_id
read -r -p 'Версия: ' offer_version
curl -fsS -H "Authorization: Bearer $employee_token" -H 'Content-Type: application/json' \
  -d "{\"version\":$offer_version}" "$api/offers/$offer_id/confirm"
```

Фоновый воркер положит письмо в локальный ящик. Введите токен участника `quality`, чтобы прочитать это письмо и ответить от имени администратора.

```sh
read -rs -p 'Токен quality: ' II_QUALITY_TOKEN
export II_QUALITY_TOKEN
python -m backend mail-list
python -m backend mail-reply '<Message-ID из списка>' 'Заявку рассматривают в течение 2 дней.'
```

Когда фоновый воркер обработает письмо, в исходной ленте появится ответ администратора. Задайте тот же вопрос от второго сотрудника в новом чате.

```sh
curl -fsS -H "Authorization: Bearer $employee_token" "$api/conversations/$chat_id/messages"
read -rs -p 'Токен employee2: ' second_token
second_chat=$(python -c 'import uuid; print(uuid.uuid4())')
curl -fsS -H "Authorization: Bearer $second_token" -H 'Content-Type: application/json' \
  -d '{"client_message_id":"example-2","text":"Сколько дней рассматривают заявку?"}' \
  "$api/conversations/$second_chat/messages"
curl -fsS -H "Authorization: Bearer $second_token" "$api/conversations/$second_chat/messages"
```

Выполните последний запрос ещё раз, если ход пока в очереди. В готовом ответе `turns[].result.sources[0].kind` равно `faq`, а `url` ведёт к опубликованному фрагменту. В почтовом имитаторе остаётся одно исходящее письмо. Имитатор хранит письма между перезапусками в `.ii-local/mail.sqlite`. Переменная `II_LOCAL_MAILBOX` задаёт другой путь. Для тестов проверяемых ссылок и переключения движков команда `python -m backend seed-fixtures --version 1` загружает синтетические документы. Второй движок выбирается значением `engine: fixture-beta` в конфигурации.

Встречный вопрос администратора продолжает ту же цепочку. Команда в ответном письме `Отзыв: вопрос снят` снимает обращение с очереди. Для уже опубликованного FAQ администратору также доступны операции замены и отзыва в API качества.

## Справка по подготовленным адаптерам

Сетевые параметры относятся к отдельному профилю `integrated`. При показе хакатона оставьте `profile` равным `hackathon`.

- **Идентификация.** Локальный каталог хранит SHA-256 непрозрачных случайных bearer-токенов и роли employee, quality, ops. Поле dion_id связывает сотрудника с UUID пользователя корпоративного DION. Параметр enabled отключает доступ. После изменения файла перезапустите API и воркеры. Сырые письма и служебные операции доступны по разным ролям. Через обратный прокси используйте HTTPS.
- **Почта.** MAIL_MODE принимает simulator, network или disabled. Для network задаются MAIL_SENDER, MAIL_RECIPIENT, MAIL_REPLY_TO, MAIL_TRUSTED_SENDERS, MAIL_AUTHSERV_ID, MAIL_SMTP_HOST/PORT/USER/PASSWORD, MAIL_IMAP_HOST/PORT/USER/PASSWORD и MAIL_MAILBOX. SMTP использует STARTTLS или MAIL_SMTP_SSL=true, IMAP использует TLS. Входной шлюз должен удалять чужие Authentication-Results со своим идентификатором и подтверждать конкретный ящик. Включение MAIL_TRUST_GATEWAY_MAILBOX_POLICY предполагает, что шлюз проверяет право отправителя выступать от имени этого адреса.
- **Документы.** В II_CONFIG поле integrations.confluence содержит base_url, spaces и token либо username/password. Передайте в spaces доступные всем сотрудникам пространства и проверьте ограничения страниц. В integrations.batch передаётся массив страниц с external_id, version, title, storage_html, опциональными space_key и acl_actor_ids. Через POST /v1/ops/documents/{source_key}/sync запустите batch или confluence и по возвращённому run_id прочитайте состояние. Текст снимка и контрольная сумма хранятся с остальными данными в PostgreSQL.
- **DION.** integrations.dion содержит base_url, email и password технического бота. public_base_url задаёт адрес для ссылок. Фоновый воркер обслуживает long polling и исходящие попытки. Контрактные тесты используют имитатор. Проверки с имитатором покрывают асинхронные квитанции, истечение токена и курсор.
- **Модель.** В демонстрации используются детерминированные имитаторы. Для реальных моделей предусмотрен API OpenRouter. В репозитории хранятся код, контракты и синтетические фикстуры. Встроенные fixture-alpha и fixture-beta реализуют AnswerEngine v1. При подключении поискового ядра оно передаёт каждый физический сетевой вызов, включая судью и повтор, через request.attempts.call с ролью вызова. Обвязка связывает попытку с ходом и trace_id. ModelAttemptResult и ModelAttemptFailure передают фактический маршрут, количество токенов и доступную стоимость. Журнал и агрегаты доступны по /v1/ops/model-calls и /v1/ops/model-costs.

## Проверка и восстановление

```sh
pip install pytest
export TEST_DATABASE_URL="$DATABASE_URL"
python -m pytest tests/acceptance/test_00_quality_loop.py
env -u II_LOOPBACK_MAIL -u II_LIVE_MODEL python -m pytest -s tests/backend tests/acceptance
python -m backend.contracts.export --check
npm ci --prefix contracts
npm run check --prefix contracts
```

Тест восстановления требует pg_dump и pg_restore версии сервера или новее и права создать временную БД. PostgreSQL-тесты создают собственные схемы. Режим `--once` у каждого воркера служит для воспроизводимого прогона одной итерации. Для штатной резервной копии используйте `pg_dump -Fc` и `pg_restore --exit-on-error` в новую БД. Тест проверяет связь истории с фактически восстановленным текстом и его хешем. Отдельная разработческая проверка реального OpenRouter включается через II_LIVE_MODEL=1 и OPENROUTER_API_KEY. Он использует одну короткую синтетическую реплику на фиксированном маршруте. Проверка локальных TLS-ящиков включается через II_LOOPBACK_MAIL=1 при запущенном стенде GreenMail.

### Отдельный стенд разработчика для SMTPS и IMAPS

В тесте участвуют бот, ящик качества и сотрудник в копии. GreenMail принимает настоящее SMTP-письмо. Тест читает его из ящиков качества и сотрудника по IMAP, отправляет обычный Reply и читает вернувшееся письмо из INBOX бота. Заголовок проверки отправителя воспроизводит утверждение доверенного шлюза. Адреса и данные синтетические.

Для отдельного локального стенда нужны Docker, keytool и свободные порты 3465/3993. Выполните в одном терминале из корня репозитория после запуска PostgreSQL.

```sh
stand=$(mktemp -d)
keytool -genkeypair -alias localhost -keyalg RSA -storetype PKCS12 \
  -keystore "$stand/loopback.p12" -storepass greenmail -keypass greenmail \
  -dname "CN=localhost" -ext SAN=DNS:localhost,IP:127.0.0.1 -validity 2
keytool -exportcert -rfc -alias localhost -keystore "$stand/loopback.p12" \
  -storepass greenmail -file "$stand/loopback.pem"
chmod 644 "$stand/loopback.p12"
mail_container=$(docker run --rm -d -p 127.0.0.1:3465:3465 -p 127.0.0.1:3993:3993 \
  -v "$stand/loopback.p12:/tmp/gm.p12:ro" \
  -e 'GREENMAIL_OPTS=-Dgreenmail.setup.test.smtps -Dgreenmail.setup.test.imaps -Dgreenmail.hostname=0.0.0.0 -Dgreenmail.users=bot:demo@sample.test,quality:demo@sample.test,first:demo@sample.test -Dgreenmail.tls.keystore.file=/tmp/gm.p12 -Dgreenmail.tls.keystore.password=greenmail' \
  greenmail/standalone:2.1.5)
SSL_CERT_FILE="$stand/loopback.pem" II_LOOPBACK_MAIL=1 python -m pytest -q tests/acceptance/test_network_mail.py
docker stop "$mail_container"
```

Фикстура для веб-клиента содержит фактические HTTP-запросы и ответы синтетического сеанса, снимок открытого согласия и SSE после возобновления с его курсора. Её можно читать без API и базы. [HTTP и SSE v1](../../tests/acceptance/fixtures/web-v1.json) · [Сценарии и версии источников AnswerEngine v1](../../backend/contracts/fixtures/v1.json)
