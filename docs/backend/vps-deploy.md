# Автодеплой ИИкузницы на VPS

Инструкция рассчитана на Linux x86‑64 с systemd, Docker Engine и Compose v2 с поддержкой `up --wait`. Скриптам на хосте нужны Bash, Git, curl, Python 3, OpenSSL, tar и flock. Рекомендуемый размер — 4 vCPU, 8 ГБ RAM и 80 ГБ SSD. На публичный IPv4 направьте A-запись домена стенда. Откройте входящие TCP 80 и 443 для Caddy и SSH для оператора. PostgreSQL, SMTP и IMAP работают во внутренних сетях Compose. Моделям нужен исходящий доступ к OpenRouter, сборке также нужны GitHub, Docker Hub, PyPI и Hugging Face.

Публичный стенд открывается на [corpdoc.single-tech.ru](https://corpdoc.single-tech.ru/). Прежний адрес `https://45.8.146.122` продолжает обслуживать существующие браузерные сессии. У каждого адреса свой сертификат и свои cookies. При первом входе по домену браузер получает отдельную сессию. [Проверка состояния стенда](https://corpdoc.single-tech.ru/healthz)

При деплое имя из `shared/hostname` выбирает файл `deploy/vps/sites/<имя>.yaml`, если он есть в устанавливаемой ревизии. Домен и прежний IP выбирают один и тот же профиль. Он задаёт публичный URL приложения и конфигурацию Caddy с автоматическим продлением обоих сертификатов. При откате применяются файлы предыдущей ревизии.

## Первичная настройка

На сервере создайте отдельного пользователя деплоя с доступом к Docker. Разместите публичный checkout в `/opt/iikuznica/repo`. Пользователь деплоя должен владеть каталогом `/opt/iikuznica`. Конфигурацию удобно установить из того же проверенного SHA, с которого запускается первый деплой.

```sh
git clone https://github.com/gureev1988-ops/iikuznica.git /opt/iikuznica/repo
install -D -m 700 /opt/iikuznica/repo/deploy/vps/check-main.sh /opt/iikuznica/bin/check-main
/opt/iikuznica/repo/deploy/vps/configure.sh /opt/iikuznica demo.example.org < /path/to/openrouter-token
```

Скрипт создаёт `shared/production.env` с правами 0600, записывает домен и генерирует пароли PostgreSQL, тестового SMTP/IMAP и служебного входа. Служебные данные `II_JUDGE_LOGIN` и `II_JUDGE_PASSWORD` передавайте закрытым каналом. Пара эксперта `II_DEMO_EXPERT_LOGIN` и `II_DEMO_EXPERT_PASSWORD` настраивается в том же файле. Отключение демовхода описано в [README](../../README.md#доступ-к-демостенду-и-смена-пароля).

Ключ OpenRouter хранится на сервере. При ротации обновите `OPENROUTER_API_KEY` в этом файле. После изменения параметров из-под пользователя деплоя повторно примените текущий зелёный выпуск командой `II_FORCE_DEPLOY=1 /opt/iikuznica/bin/check-main`.

## Автоматическое обновление

На VPS обработчик диалогов использует один поток ONNX на оператор MiniLM. Одновременные диалоги могут выполнять ранжирование на разных ядрах. Число потоков на оператор задаётся через `II_MINILM_INTRA_THREADS` в `shared/production.env`. В журнале `interactive` строка `stage=interactive_start` показывает применённое число потоков, доступные по affinity ядра и квоту cgroup v2.

Установите таймер и сервис на VPS. В примере пользователь деплоя называется `iikdeploy`.

```sh
sudo install -m 644 /opt/iikuznica/repo/deploy/vps/iikuznica-deploy@.service /etc/systemd/system/
sudo install -m 644 /opt/iikuznica/repo/deploy/vps/iikuznica-deploy@.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now iikuznica-deploy@iikdeploy.timer
sudo systemctl start iikuznica-deploy@iikdeploy.service
```

GitHub Actions запускает `core` и `backend` на каждом обновлении `main`. Таймер проверяет текущий SHA примерно раз в две минуты. VPS допускает к сборке только SHA из `main` с успешным публичным запуском Core checks. Старые SHA и результаты PR не подходят. Сервер сам скачивает код, поэтому GitHub Actions Secrets, приватный токен GitHub и входящий SSH из Actions не требуются. Первичный запуск также возможен после ручного запуска Core checks на `main`.

```sh
sudo journalctl -u iikuznica-deploy@iikdeploy.service -n 100 --no-pager
curl -fsS -D - https://demo.example.org/healthz
```

В заголовке `X-IIKuznica-Revision` указан полный SHA запущенного выпуска. Таймер повторяет проверку, пока Core checks для нового SHA ожидает результата.

## Проверка выпуска и восстановление

На VPS используется постоянный Compose-проект `iikuznica-prod`. Подготовка нового выпуска отдельно собирает образы и проверяет три модельных маршрута с серверными ключами. Перед сменой версии сохраняется `pg_dump`. После запуска проверяются публичный HTTPS и оба почтовых ящика. Данные, оригиналы, MiniLM, почта и сертификаты переживают пересборку в именованных томах.

При ошибке после смены версии скрипт пытается вернуть сервисы предыдущего SHA на тех же томах. Откат образов и восстановление PostgreSQL выполняют разные операции. Если миграция несовместима с предыдущим приложением, сначала сохраните текущее состояние и отдельно согласуйте восстановление из копии. До проверки сохранности не удаляйте предыдущий выпуск, образы и резервную копию. Ход сборки и результат публикации видны в журнале systemd.

После первого выпуска проверьте точную цитату, уточнение и ответ эксперта через учебную почту. [Проверка опубликованного стенда](assembly-run.md#после-публикации-на-публичном-стенде)
