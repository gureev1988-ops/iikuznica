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
- **Почта.** MAIL_MODE принимает simulator, network или disabled. Для network задаются MAIL_SENDER, MAIL_RECIPIENT, MAIL_REPLY_TO, MAIL_TRUSTED_SENDERS, MAIL_AUTHSERV_ID, MAIL_SMTP_HOST/PORT/USER/PASSWORD, MAIL_IMAP_HOST/PORT/USER/PASSWORD и MAIL_MAILBOX. Получатель и единственный доверенный отправитель в профиле `integrated` — `quality@softline.ru`. SMTP использует STARTTLS или MAIL_SMTP_SSL=true, IMAP использует TLS. Входной шлюз должен удалять чужие Authentication-Results со своим идентификатором и подтверждать конкретный ящик. Включение MAIL_TRUST_GATEWAY_MAILBOX_POLICY предполагает, что шлюз проверяет право отправителя выступать от имени этого адреса.
- **Документы.** Старые `integrations.confluence` и `integrations.batch` сохраняют ключи `confluence` и `batch` и запускаются вручную. Настройка новых выгрузок и проверка импорта описаны в [инструкции оператора](#как-подключить-ещё-один-источник).
- **DION.** integrations.dion содержит base_url, email и password технического бота. public_base_url задаёт адрес для ссылок. Фоновый воркер обслуживает long polling и исходящие попытки. Контрактные тесты используют имитатор. Проверки с имитатором покрывают асинхронные квитанции, истечение токена и курсор.
- **Модель.** В демонстрации используются детерминированные имитаторы. Для реальных моделей предусмотрен API OpenRouter. В репозитории хранятся код, контракты и синтетические фикстуры. Встроенные fixture-alpha и fixture-beta реализуют AnswerEngine v1. При подключении поискового ядра оно передаёт каждый физический сетевой вызов, включая судью и повтор, через request.attempts.call с ролью вызова. Обвязка связывает попытку с ходом и trace_id. ModelAttemptResult и ModelAttemptFailure передают фактический маршрут, количество токенов и доступную стоимость. Журнал и агрегаты доступны по /v1/ops/model-calls и /v1/ops/model-costs.

## Как подключить ещё один источник

Владелец публикует согласованный каталог документов в JSON. Оператор добавляет подключение, проверяет доступ и назначает интервал. ИИкузница ведёт версии и обновляет каждый каталог независимо. Новый формат подключается отдельным адаптером.

### Подготовка каталога

Назначьте владельца и согласуйте периодичность. Включите только материалы, которые может читать каждый сотрудник, и проверьте ссылки на оригиналы. Для смешанных прав нужен адаптер с поддержкой ACL.

Размещайте один полный UTF-8 файл по согласованному HTTPS-адресу. Публикуйте его атомарно после сборки. Если выгрузка неполная, верните ошибку и сохраните предыдущий выпуск. Пример каталога v1.

```json
{
  "schema_version": 1,
  "snapshot_revision": 14,
  "complete": true,
  "count": 1,
  "documents": [
    {
      "id": "instruction-17",
      "revision": 2,
      "title": "Порядок регистрации заявки",
      "text": "Сотрудник регистрирует заявку в журнале.",
      "source_url": "https://kb.example.test/instructions/17"
    }
  ]
}
```

- Сохраняйте `id` при переименовании. При изменении заголовка, текста или `source_url` повышайте `revision` документа и `snapshot_revision` каталога. При добавлении или исключении документа также повышайте `snapshot_revision`. Для возврата к прежнему содержимому выпускайте новые версии. Порядок записей и полей можно менять без повышения версии.
- Указывайте `count` равным длине `documents`. Схема принимает ровно показанные поля, целые положительные номера и абсолютные HTTPS-ссылки. Лимиты — 500 документов и 2 МиБ после распаковки. Запросу отведено 10 секунд. Ссылки служат для атрибуции и хранятся в редакции.
- Штатная проверка источника читает весь файл. Импорт дополнительно сверяет выпуск и версии документов с PostgreSQL, включая историю исключённых записей. После проверки запуск закрепляет снимок за своим `run_id`. Обычное индексирование публикует редакции и фрагменты как `document`. После сбоя очередь дочитывает закреплённый снимок. По успешному завершению в базе остаются номер и контрольная сумма выпуска, редакции и фрагменты.

### Настройка и первая проверка

Используйте `integrated` с отдельными конфигурацией и PostgreSQL. Добавьте блок `integrations.document_sources` в файл `II_CONFIG`, сохранив действующих пользователей и остальные настройки. Выберите постоянный уникальный ключ для этого корпуса. Для второго каталога добавьте соседнюю запись с собственными ключом, адресом и переменной токена. Ключ `synthetic-v1` и ключ `batch` зарезервированы. Занятый ключ прежнего подключения сохраняется за этим подключением. Для другого набора данных создавайте новый ключ.

```json
{
  "profile": "integrated",
  "integrations": {
    "document_sources": {
      "instructions": {
        "type": "http_json",
        "enabled": true,
        "interval_seconds": null,
        "settings": {
          "url": "https://feeds.example.test/instructions.json",
          "bearer_token_env": "II_INSTRUCTIONS_READ_TOKEN",
          "audience": "all_employees"
        }
      }
    }
  }
}
```

Выдайте токен только на чтение и задайте его в `II_INSTRUCTIONS_READ_TOKEN` в окружении API и фонового воркера. Удалите поле `bearer_token_env`, если согласованный адрес доступен анонимно внутри контура. Значение токена передавайте через окружение. Для именованного `confluence` перенесите настройки из `integrations.confluence` в `settings`, включая `base_url`, `spaces` и необходимые учётные данные. Для именованного `batch` передайте старый массив страниц в `settings.pages`.

Из корня репозитория примените миграции. Запустите API и `background` в отдельных процессах с одинаковым конфигом и окружением. Если процессы уже работают, перезапустите их.

```sh
python -m backend migrate
python -m backend api
python -m backend background
```

Задайте `API_BASE` без суффикса `/v1`. В `OPS_TOKEN` передайте токен учётной записи с ролью `ops`.

```sh
curl -fsS -H "Authorization: Bearer $OPS_TOKEN" "$API_BASE/v1/ops/documents/connections"
curl -fsS -X POST -H "Authorization: Bearer $OPS_TOKEN" "$API_BASE/v1/ops/documents/instructions/check"
curl -fsS -X POST -H "Authorization: Bearer $OPS_TOKEN" "$API_BASE/v1/ops/documents/instructions/sync"
read -r -p 'run_id из ответа 202: ' RUN_ID
curl -fsS -H "Authorization: Bearer $OPS_TOKEN" "$API_BASE/v1/ops/documents/sync/$RUN_ID"
```

Дождитесь `completed`. В списке подключений проверьте `last_success_at`, `active_documents` и последний `last_run`. У `check` свой результат `ok`, `checked_at` и `error_code`. Время последнего успешного импорта отражает актуальность корпуса. До первой проверки или успешного импорта соответствующие поля равны `null`.

Возьмите оба ID из контрольной цитаты или выполните следующий запрос в тестовой БД через `psql`. Значение `TEST_DATABASE_URL` должно указывать на ту же тестовую базу, куда прошёл импорт.

```sh
psql "$TEST_DATABASE_URL" -X -v ON_ERROR_STOP=1 \
  -v source_key=instructions -v external_id=instruction-17 <<'SQL'
SELECT r.id AS revision_id, f.id AS fragment_id, r.upstream_url, f.text
FROM sources s
JOIN source_revisions r ON r.id = s.current_revision_id
JOIN source_fragments f ON f.revision_id = r.id
WHERE s.source_system = 'document' AND s.status = 'active'
  AND s.source_key = :'source_key' AND s.external_id = :'external_id'
ORDER BY f.ordinal;
SQL
read -r -p 'revision_id: ' REVISION_ID
read -r -p 'fragment_id: ' FRAGMENT_ID
curl -fsS -H "Authorization: Bearer $EMPLOYEE_TOKEN" \
  "$API_BASE/v1/sources/$REVISION_ID/fragments/$FRAGMENT_ID"
```

В `EMPLOYEE_TOKEN` передайте токен сотрудника. Сверьте точный текст контрольного фрагмента, `kind: document` и адрес оригинала из `upstream_url`. Повторите импорт без изменений и проверьте прежние `revision_id` и `fragment_id`. Затем измените один документ с повышением обеих версий и проверьте новый фрагмент. После ручной приёмки задайте нужной записи `interval_seconds: 86400` или иной целочисленный интервал от 60 секунд и перезапустите процессы. Первый срок появится после регистрации фоновым воркером и будет отстоять на заданный интервал. `next_due_at` указывается в UTC. Повторный ручной запуск расписание сохраняет.

### Ошибки, пауза и отзыв

| Наблюдение | Действие |
|---|---|
| `catalog_configuration`, `http_unauthorized` | Проверьте переменную токена в окружении обоих процессов, срок действия и право чтения. |
| `http_timeout`, `http_transport`, `http_status`, `http_redirect` | Проверьте DNS, TLS и доступность источника из контура приложения. Укажите конечный HTTPS-адрес, который отвечает `200` без перенаправления. |
| `invalid_json`, `invalid_catalog`, `invalid_document`, `duplicate_document`, `too_many_documents`, `invalid_encoding`, `response_too_large` | Исправьте схему, полноту, версии, кодировку или размер. Повторите `check`, затем `sync`. |
| `snapshot_regression`, `snapshot_conflict`, `document_version_regression`, `document_version_conflict` | Сверьте опубликованные выпуски владельца. Опубликуйте корректный новый выпуск. Для каждого изменённого документа повысьте его версию. `check` подтверждает доступ и схему, а сравнение с каталогом выполняется при `sync`. |
| `incomplete` | Сверьте `last_run.error_code` и `last_success_at`. Очередь ограничивает число повторов. Ручной запуск или следующий срок возобновляет тот же `run_id`. Если снимок уже закреплён, обновлённый файл будет прочитан следующим запуском после завершения текущего. |
| `403`, `404`, `409` на операторском маршруте | Проверьте роль `ops`, профиль и ключ подключения. Для явно приостановленного подключения `check` и `sync` возвращают `409`. |

Для паузы дождитесь завершения активного запуска, укажите `enabled: false` и перезапустите процессы. Следующий срок снимается, опубликованные документы остаются доступны. Чтобы снять документы, сначала опубликуйте новый полный выпуск без нужных записей и дождитесь `completed`. Для снятия всего корпуса этого ключа опубликуйте новый выпуск с `complete: true`, `count: 0` и `documents: []`.

Если у прикладной системы нет экспорта v1, владелец может разместить преобразователь на своей стороне. Согласуйте область чтения, устойчивое соответствие ID, версии изменений и ссылки на оригиналы. Для другого протокола реализуйте `DocumentSource.list_pages` и `probe`, зарегистрируйте фабрику адаптера и добавьте проверку связи. В приёмке проверьте повторный импорт, изменение, отзыв и открытие контрольной ссылки от имени сотрудника.

Автоматическая приёмка использует PostgreSQL и синтетические HTTP-ответы. На стороне заказчика отдельно проверьте HTTPS и токен из контуров API и воркера, общедоступность согласованного набора документов, ссылки на оригиналы под ролью сотрудника, монотонность реальной выгрузки и два полных цикла с расписанием. При выборе Confluence проверьте также права на пространства и чтение страниц. В `hackathon` остаются локальный корпус и прежний ручной `batch`.

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
