# ИИкузница

«Демо DION» отвечает на вопросы о пресейле и проектной деятельности по четырём учебным регламентам. В браузере можно открыть процитированный пункт нужной редакции, ответить на уточнение и передать вопрос в Отдел качества. Вход в общий тестовый ящик открывает «Входящие письма» и Reply.

## План пилота в Softline

Пилот рассчитан на группу из 20–30 сотрудников и корпус из 100+ регламентов. Предлагаемый ориентир — сократить медианное время до проверенного ответа на 30% относительно текущего E-Bot при сохранении точности.

[План пилота, сроки и ответственные](docs/pilot-softline.md) · [ТЗ первого экрана «Тех.раздела»](docs/design/softline-pilot-section.md) · [Локальные модели в пилоте](docs/pilot-softline.md#локальные-модели-в-контуре-softline)

[Финальный круг доработок](docs/design/final-refinements.md)

## Запуск

Для публичного стенда доступен [автодеплой из main на VPS](docs/backend/vps-deploy.md).

Нужны Docker Compose v2, OpenSSL и переменная окружения `OPENROUTER_API_KEY` с доступом к Gemma, Qwen и OpenRouter Decisions. Из корня чистого checkout:

```sh
./deploy/init-env.sh
docker compose up -d --build
```

Откройте `https://localhost:4443`. Для локального HTTPS Caddy создаёт собственный CA. Сертификат CA можно извлечь командой:

```sh
docker compose exec -T https cat /data/caddy/pki/authorities/local/root.crt > local-ca.pem
curl --cacert local-ca.pem https://localhost:4443/healthz
```

Стартовый контейнер применяет миграции, проверяет MiniLM и импортирует оригиналы. API и два воркера ждут окончания импорта. Состояние PostgreSQL, оригиналы, почта и веса хранятся в именованных томах. Учётные данные тестового ящика и браузерного входа создаются в локальном `.env`. Выдавайте судьям только `II_JUDGE_LOGIN` и `II_JUDGE_PASSWORD` по закрытому каналу.

Для внешнего HTTPS укажите `II_PUBLIC_URL=https://ваш-домен`, `II_CADDY_ADDRESS=ваш-домен`, `II_CADDY_TLS=tls ваше-почтовое-имя`, `II_LISTEN_IP=0.0.0.0`, `II_HTTPS_PORT=443`. DNS должен вести на узел стенда, а порт 443 — быть доступен для HTTPS и ACME. SMTP и IMAP находятся в отдельной внутренней сети Compose.

Интеграционный профиль `assembly` использует A1, OpenRouter и Dovecot. Сведения о `hackathon`, учебных сообщениях и прежнем приоритете FAQ в веточных документах относятся к историческим проверкам. Для текущего выпуска действуют спецификация сборки и правило приоритета утверждённого регламента.

- [Спецификация сборки](docs/architecture/assembly-v1.md)
- [Бриф и спецификация поиска](docs/IIkuznitsa_Sprint_Brief.md)
- [Приоритет регламента](docs/architecture/faq-regulation-priority.md)
- [Команды технической проверки](docs/backend/assembly-run.md)
