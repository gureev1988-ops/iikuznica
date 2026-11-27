# Как участвовать в разработке

Если ассистент дал неверный ответ, укажите в [Issues](https://github.com/gureev1988-ops/iikuznica/issues) учебный вопрос, фактический ответ и пункт регламента, который должен был увидеть пользователь. Для ошибки в интерфейсе пригодятся роль пользователя и шаги воспроизведения.

Предлагайте изменения отдельным pull request в `main`. Опишите, что изменится для пользователя или разработчика, и укажите, как это проверить. Команды ниже выполняются из корня репозитория.

## Поиск и учебные данные

Для этих проверок достаточно Python 3.12 и Node.js. Они работают локально, без ключа модели и запущенного стенда.

```sh
python3 eval/registry/registry.py --build
python3 eval/registry/registry.py --check
python3 -m unittest discover -s eval/scope
python3 eval/search_core/check_permission.py
node --test demo/faq-revisions/model.test.cjs
node eval/faq-priority/evaluate.cjs
```

Первые две команды собирают реестр из четырёх оригиналов DOCX и сверяют ссылки с пунктами документов. При замене оригинала пересмотрите соответствующую [разметку](eval/registry/annotations/) перед обновлением [контрольной суммы](eval/registry/versions.json).

## Бэкенд и API

Для проверок бэкенда нужны Python 3.12, PostgreSQL 16 и клиентские утилиты `pg_dump` и `pg_restore`. Подключите отдельную тестовую базу. Тестовая роль должна уметь создавать схемы, а проверка восстановления резервной копии создаёт временную базу и требует права `CREATEDB`.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -r requirements-assembly.txt pytest

# Пример для локальной тестовой базы с параметрами из CI
export TEST_DATABASE_URL='postgresql://postgres:postgres@127.0.0.1:5432/postgres'
python -m pytest -q -rs tests/backend tests/acceptance
```

Ключ `-rs` показывает причины пропусков. По умолчанию тесты с реальным вызовом модели (`II_LIVE_MODEL`) и отдельным почтовым сервером (`II_LOOPBACK_MAIL`) пропускаются.

При изменении публичного API обновите схему OpenAPI и типы TypeScript в том же pull request. Генератор использует установленное Python-окружение, Node.js и npm. [Генерация и проверка контрактов](contracts/README.md)

Точные команды для проверок перед слиянием приведены в [Core checks](.github/workflows/core-checks.yml). После успешной проверки `main` отдельный процесс [Published stand smoke](.github/workflows/release-smoke.yml) проверяет опубликованную ревизию и гостевой чат.
