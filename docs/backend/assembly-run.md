# Проверки сборки

Команды выполняются из корня репозитория. Для проверки кода достаточно локальной базы и тестовых данных. Браузерную приёмку и замеры ответов запускают отдельно, на работающем стенде с ключом OpenRouter.

## Проверки кода

Для проверок поиска, PostgreSQL и контракта API подготовьте Python-окружение и отдельную тестовую базу по [инструкции разработчика](../../CONTRIBUTING.md). Те же сценарии выполняет [Core checks](../../.github/workflows/core-checks.yml).

Для отдельной проверки сетевого адаптера модели задайте `II_LIVE_MODEL=1` и `OPENROUTER_API_KEY` при запуске [теста OpenRouter](../../tests/acceptance/test_model_network.py).

Для проверки переключения на внутренние Gemma, Qwen и Decider используйте [инструкцию оператора локального профиля](local-models.md).

## Локальный стенд

Поднимите [сборку Compose](../../README.md) в отдельной копии репозитория со своим файлом `.env`, томами и свободным HTTPS-портом. Для изолированного набора томов задайте отдельное имя проекта, например `export COMPOSE_PROJECT_NAME=iikuznica-qa`, до запуска Compose. Браузерный сценарий создаёт тестовые обращения, отвечает на письма и на время останавливает SMTP. Начните со свежих данных этого проекта. Дальнейшие команды предполагают свободный локальный порт 4443 и Python-окружение `.venv` из инструкции разработчика.

```sh
docker compose ps -a
docker compose exec -T https cat /data/caddy/pki/authorities/local/root.crt > local-ca.pem
curl --cacert local-ca.pem https://localhost:4443/healthz
docker compose exec -T interactive python - < deploy/qa/models.py

.venv/bin/python -m pip install playwright
.venv/bin/python -m playwright install chromium
II_BROWSER_URL=https://localhost:4443 II_BROWSER_LOCAL_CA=1 \
  II_BROWSER_COMPOSE="docker compose" .venv/bin/python -m pytest -s tests/browser

.venv/bin/python deploy/qa/load.py --ca local-ca.pem --count 1
.venv/bin/python deploy/qa/load.py --ca local-ca.pem --count 10
```

Успешный `init` завершается с кодом 0. Если сервисы ожидают запуска, посмотрите `docker compose logs init`. В `/healthz` ожидаются `database: ready` и четыре регламента. Скрипт `models.py` проверяет рабочие маршруты моделей. Скрипт `load.py` создаёт новые гостевые сессии и показывает число завершённых ходов, число ответов с источником и задержки p50/p95 для этих ответов. Параметр `II_BROWSER_LOCAL_CA=1` используется только для локального теста Playwright.

## Внешний HTTPS в собственном Compose

Для своего домена направьте DNS на узел с Caddy и откройте входящий TCP 443, в том числе для проверки ACME TLS-ALPN. В локальном `.env` замените домен и контактный адрес на свои.

```dotenv
II_PUBLIC_URL=https://demo.example.org
II_CADDY_ADDRESS=demo.example.org
II_CADDY_TLS="tls ops@example.org"
II_LISTEN_IP=0.0.0.0
II_HTTPS_PORT=443
```

Примените настройки командой `docker compose up -d`, затем проверьте `curl -fsS https://demo.example.org/healthz` со своего компьютера. Сертификат и ключ Caddy сохраняет в именованном томе `caddy`. [Конфигурация Compose](../../compose.yaml) · [Настройки Caddy](../../deploy/Caddyfile)

## После публикации на публичном стенде

[Published stand smoke](../../.github/workflows/release-smoke.yml) запускается после успешного Core checks для актуального SHA ветки `main`. Проверка сверяет заголовок `X-IIKuznica-Revision` до и после сценария, состояние корпуса, изоляцию гостевых сессий, открытие оригиналов, общие экраны и историю диалога. Автоматический запуск отправляет один непрофильный вопрос в собственном тестовом чате.

Ручной запуск из Actions добавляет содержательные ответы и проверку цитат. Воспроизвести его можно из проверяемой ревизии с установленными pytest, Playwright и Chromium. Укажите в `RELEASE_SHA` полный SHA прошедшего CI коммита.

```sh
.venv/bin/python deploy/qa/release_smoke.py --url https://corpdoc.single-tech.ru \
  --sha "$RELEASE_SHA" --wait 1200 --browser --roundtrip --answers
```

Флаг `exercise_mail` в Actions или `--mail` в CLI добавляет одно учебное обращение, ответ эксперта и проверку получившегося FAQ в другой сессии. Для изменённых учётных данных задайте `II_SMOKE_EXPERT_LOGIN` и `II_SMOKE_EXPERT_PASSWORD`. Проверка применяет учебный вход `admin` / `admin` только когда сам стенд сообщает, что он включён.

## Сетевая почта в GreenMail

В тесте участвуют бот, ящик качества и сотрудник в копии. GreenMail принимает настоящее SMTP-письмо. Тест читает его из ящиков качества и сотрудника по IMAP, отправляет обычный Reply и читает вернувшееся письмо из INBOX бота. Заголовок проверки отправителя воспроизводит утверждение доверенного шлюза. Адреса и данные синтетические.

Для отдельного локального стенда нужны Docker, keytool и свободные порты 3465/3993. Выполните в одном терминале из корня репозитория после подготовки тестовой БД и экспорта `TEST_DATABASE_URL`.

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
SSL_CERT_FILE="$stand/loopback.pem" II_LOOPBACK_MAIL=1 .venv/bin/python -m pytest -q tests/acceptance/test_network_mail.py
docker stop "$mail_container"
```

## Редакции и резервная копия

Исходная редакция регламентов имеет номер 1. При смене исходников в `corpus/` пересоберите образ с новыми DOCX и запустите `docker compose run --rm init assembly-init --version N`, подставив номер новой редакции. Индексатор публикует её после сохранения фрагментов. Прежние цитаты указывают на свои редакции.

Сохраните PostgreSQL через `pg_dump -Fc`, а также тома оригиналов и почты. Восстанавливайте в отдельные тома и новую БД через `pg_restore --exit-on-error`. Проверьте миграции, историю и открытие прежней цитаты. [Обновление и откат на VPS](vps-deploy.md) · [Подключение внешнего источника](run.md#как-подключить-ещё-один-источник)
