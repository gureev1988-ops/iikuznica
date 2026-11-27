# Проверка стенда ИИкузницы

Запускайте браузерное испытание на выделенном экземпляре с отдельными томами и пустым FAQ. Сценарий отказа временно останавливает SMTP на этом экземпляре. Не используйте базу показа.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-assembly.txt pytest playwright
.venv/bin/python -m playwright install chromium
docker compose exec -T interactive python - < deploy/qa/models.py
II_BROWSER_URL=https://localhost:4443 II_BROWSER_LOCAL_CA=1 \
  II_BROWSER_COMPOSE="docker compose" .venv/bin/python -m pytest -s tests/browser
.venv/bin/python deploy/qa/load.py --ca local-ca.pem --count 1
.venv/bin/python deploy/qa/load.py --ca local-ca.pem --count 10
```

Параметр `II_BROWSER_LOCAL_CA=1` разрешает локальный сертификат только в контексте Playwright. Для стенда с публичным сертификатом пропустите параметр. Браузер и API используют один origin. Команда `/healthz` проверяет PostgreSQL и готовность корпуса. Доступность моделей и почты проверяется отдельными сценариями.

В инфраструктуре с собственным CA используйте сборочный секрет `pip_ca` и смонтируйте доверенный CA в контейнеры `init` и `interactive` с `SSL_CERT_FILE`. CA и параметры прокси конкретной площадки не выносятся в образ.

## Проверки кода

```sh
python3 eval/registry/registry.py --build
python3 eval/registry/registry.py --check
python3 -m unittest discover -s eval/scope
python3 eval/search_core/check_permission.py
.venv/bin/python -m backend.contracts.export --check
npm ci --prefix contracts
npm run check --prefix contracts
TEST_DATABASE_URL=postgresql://... .venv/bin/python -m pytest -s tests/backend tests/acceptance
```

Для PostgreSQL-тестов у тестового пользователя должно быть право создавать временные схемы и БД. Для восстановления нужны клиентские `pg_dump` и `pg_restore` версии сервера или новее. Контролируемая нагрузка 20/200 в `test_burst.py` использует fixture-engine и PostgreSQL. Она измеряет очередь и восстановление без внешних модельных расходов. Реальные ответы 1/10 измеряет `deploy/qa/load.py`. Фактически обслужившие маршруты и usage берутся из `model_calls` по идентификаторам хода.

## Версии и восстановление

Начальная редакция каждого регламента — 1. При импорте другой редакции подготовьте оригиналы и выполните `python -m backend assembly-init --version N` с общим томом оригиналов. База активирует редакцию только после сохранения фрагментов и postings. Ранее процитированный фрагмент остаётся доступен по своему идентификатору редакции при сохранении прав. Проверенная разметка применяется только при совпадении хеша оригинала.

Сохраните PostgreSQL через `pg_dump -Fc`, а также тома оригиналов и почты. Восстанавливайте в отдельные тома и новую БД через `pg_restore --exit-on-error`. Затем проверьте миграции, исходную историю и открытие той же ссылки. Оригиналы и вес MiniLM проверяются по SHA-256.

Сетевые Confluence и HTTPS/JSON используют именованные подключения и зафиксированный снимок. Формат страниц, авторизация и расписание описаны в [спецификации источников](../architecture/document-sources-v1.md). DION Bot API остаётся отдельным адаптером. Профиль `assembly` обслуживает браузерный «Демо DION».
