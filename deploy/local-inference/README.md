# GPU-узел пилота Softline

Этот каталог разворачивает три HTTP-сервиса для [локального профиля приложения](../../docs/backend/local-models.md). Сервисы моделей читают файлы из внутреннего хранилища и подключены только к изолированной сети Docker. Единственная точка входа на GPU-узел — HAProxy с тремя фиксированными HTTP-маршрутами. При включении приложения на другом узле ИТ публикует эти маршруты через корпоративный HTTPS-шлюз.

| Роль и alias API | Источник локальных весов | Сервер и порт на GPU-узле |
| --- | --- | --- |
| Gemma, `google/gemma-4-26b-a4b-it` | [Google Gemma 4 26B A4B IT](https://huggingface.co/google/gemma-4-26B-A4B-it/tree/4d7ae4984b7db7de8f8457170b3f1a419ee76d52), [версия после QAT](https://huggingface.co/google/gemma-4-26B-A4B-it-qat-q4_0-unquantized/tree/f1e06dc520982d9b9edd76859fdb7ab209449949), [официальный Q4_0 GGUF](https://huggingface.co/google/gemma-4-26B-A4B-it-qat-q4_0-gguf/tree/d1c082be9cf3c8a514acf63b8761f4b41935842e). Для текстовых запросов используется один файл весов. | llama.cpp b12808, CUDA 12, `18081/v1/chat/completions` |
| Qwen, `qwen/qwen3.5-122b-a10b` | [Qwen3.5-122B-A10B](https://huggingface.co/Qwen/Qwen3.5-122B-A10B/tree/dc4d348443bc740c68e2d77492492c11606384d5) и [официальный GPTQ-Int4](https://huggingface.co/Qwen/Qwen3.5-122B-A10B-GPTQ-Int4/tree/30cd92cba9707a9aba09d1e490ed4b66b78e9606). Все 39 частей весов, конфигурация и токенизатор из одной ревизии. | vLLM 0.34.0, `moe_wna16`, текстовый режим, `18082/v1/chat/completions` |
| Decider, `perplexity/pplx-decider-flash-v1.3` | [Perplexity Flash v1.3](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.3/tree/3df8d577c17facbdc995536bfb65785f1582a1e6). Профиль BF16 использует backbone, readout, конфигурацию протокола и штатный `pplx_decider` версии 0.5.0 из `source/src`. | [HTTP-сервис из этого репозитория](decider/server.py), `18083/v1/decisions` |

В карточках всех трёх артефактов указана Apache-2.0. Сохраните карточки моделей, файлы лицензий и NOTICE при переносе в корпоративное хранилище. Подтвердите качество выбранных квантизаций на контрольных вопросах. [Параметры llama.cpp](https://github.com/ggml-org/llama.cpp/blob/ed2153e708de813c35559102aed43b3085179c27/tools/server/README.md) · [Рецепт Qwen для vLLM](https://huggingface.co/Qwen/Qwen3.5-122B-A10B-GPTQ-Int4/blob/30cd92cba9707a9aba09d1e490ed4b66b78e9606/README.md) · [Нативный протокол Decider](https://huggingface.co/perplexity-ai/pplx-decider-flash-v1.3/blob/3df8d577c17facbdc995536bfb65785f1582a1e6/README.md)

## Подготовить поставку

1. На стороне подготовки поместите файлы указанных ревизий в подкаталоги `gemma/`, `qwen/` и `decider/`, сохраняя внутренние имена и пути. [SHA256SUMS](SHA256SUMS) перечисляет весь комплект из 78 файлов, включая исходник Decider и токенизаторы. Для весов закреплены SHA-256 объектов Git LFS, для служебных файлов — SHA-256 содержимого фиксированной ревизии. Перенесите комплект во внутреннее хранилище. Весовые файлы трёх моделей занимают около 102 ГБ на диске.

   На машине с разрешённым доступом к источнику и CLI `hf` выполните из корня репозитория команды Bash. В CLI передаётся точный список файлов каждой роли из манифеста. Для предварительного просмотра добавьте `--dry-run` в команду `hf download`. Переменная `weights_staging` указывает на каталог подготовки с достаточным местом.

   ```bash
   weights_staging=/srv/ii-weights
   fetch_role() {
     local role="$1" repo="$2" revision="$3"
     local -a files
     mapfile -t files < <(awk -v prefix="$role/" 'index($2,prefix)==1 {print substr($2,length(prefix)+1)}' deploy/local-inference/SHA256SUMS)
     if (( ${#files[@]} == 0 )); then return 1; fi
     hf download "$repo" "${files[@]}" --revision "$revision" --local-dir "$weights_staging/$role"
   }
   fetch_role gemma google/gemma-4-26B-A4B-it-qat-q4_0-gguf d1c082be9cf3c8a514acf63b8761f4b41935842e
   fetch_role qwen Qwen/Qwen3.5-122B-A10B-GPTQ-Int4 30cd92cba9707a9aba09d1e490ed4b66b78e9606
   fetch_role decider perplexity-ai/pplx-decider-flash-v1.3 3df8d577c17facbdc995536bfb65785f1582a1e6
   ```

2. Подготовьте Linux/amd64, Docker Engine с Compose v2.30 или новее, NVIDIA Container Toolkit и совместимый NVIDIA-драйвер. Проверяйте совместимость всех трёх образов с конкретными GPU, включая ядра `moe_wna16` для Qwen и CUDA 13 в зависимостях Decider. ИТ задаёт список доступных GPU, параллелизм, длину контекста и долю памяти Qwen. Если Qwen распределена по нескольким GPU, `II_QWEN_TP` должно совпадать с числом устройств в `II_QWEN_GPUS`.
3. Зеркалируйте опубликованные образы целиком с сохранением digest индекса либо заранее загрузите их по digest в Docker Engine на целевом узле. В [Compose](compose.yaml) закреплены [llama.cpp CUDA](https://github.com/ggml-org/llama.cpp/pkgs/container/llama.cpp), [vLLM 0.34.0](https://hub.docker.com/r/vllm/vllm-openai/tags?name=v0.34.0) и [HAProxy 3.2 Alpine](https://hub.docker.com/_/haproxy). Для реестра Softline изменяются только имена репозиториев в файле окружения. Compose использует заранее подготовленные образы с `pull_policy: never`.
4. На машине сборки соберите Decider из [Dockerfile](decider/Dockerfile). В нём закреплён digest базового Python, версии HTTP-сервера, Torch, Transformers и зависимостей SDK перечислены в [requirements.txt](decider/requirements.txt). Полный набор колёс Linux/amd64 для Python 3.12 и их SHA-256 фиксирует [requirements.lock](decider/requirements.lock). Для повторного расчёта используйте uv 0.13.9 и команду `python3.12 deploy/local-inference/decider/lock_requirements.py` из корня репозитория. Для сборки нужен доступ к зеркалу пакетов или разрешённому источнику пакетов. Для внутреннего CA передайте Docker BuildKit `--secret id=pip_ca,src=/путь/ca.pem`. Уже собранный образ переносится в закрытый контур. Запишите digest результата, версию исходного кода приложения и сохраните их вместе с релизом. Команда выполняется из корня репозитория.

```sh
decider_repo=registry.example.internal/ii/decider
docker buildx build --platform linux/amd64 --push \
  --metadata-file /tmp/ii-decider-build.json \
  --tag "$decider_repo:pilot-v1" deploy/local-inference/decider
jq -r '."containerimage.digest"' /tmp/ii-decider-build.json
```

5. Из корня репозитория скопируйте [pilot.env.example](pilot.env.example) в закрытый файл. Заполните `II_DECIDER_IMAGE_REPOSITORY` и `II_DECIDER_IMAGE_SHA256` (64 знака hex из результата сборки без префикса `sha256:`), абсолютный путь `II_WEIGHTS_DIR`, привязки устройств и три разных токена. Для числа устройств и доли памяти в примере приведена схема совместного размещения на одной карте. Ключи приложения задаются как `II_LOCAL_GEMMA_TOKEN`, `II_LOCAL_QWEN_TOKEN`, `II_LOCAL_DECIDER_TOKEN`; каждому соответствует одноимённая роль с `II_*_API_KEY` на сервере. На GPU-узле дайте процессам контейнеров право читать файлы, включая UID 65532 сервиса Decider.

```sh
umask 077
mkdir -p .ii-local
cp deploy/local-inference/pilot.env.example .ii-local/gpu.env
chmod a+r deploy/local-inference/haproxy.cfg
```

## Проверка и запуск

Команды выполняются из корня репозитория. Сначала заполните файл окружения. Прокси читает общедоступный файл маршрутов под пользователем `haproxy`. Разрешите чтение файлов весов и проход по каталогам внутри каждого монтируемого подкаталога для UID соответствующего сервиса. Включите `decider/source/src/` в эту проверку. Проверка контрольных сумм требует подготовленных файлов и читает весь комплект с диска.

```sh
weight_manifest="$PWD/deploy/local-inference/SHA256SUMS"
(cd /srv/ii-weights && sha256sum --check --strict "$weight_manifest")
gpu_compose() {
  docker compose --env-file .ii-local/gpu.env -f deploy/local-inference/compose.yaml "$@"
}
gpu_compose config --quiet
gpu_compose config --images
gpu_compose config --images | while IFS= read -r ref; do docker image inspect "$ref" >/dev/null || exit 1; done
gpu_compose up -d
gpu_compose ps
curl --fail http://127.0.0.1:18081/health
curl --fail http://127.0.0.1:18082/health
curl --fail http://127.0.0.1:18083/health
```

Подставьте фактический `II_WEIGHTS_DIR` вместо `/srv/ii-weights`. Пока модели загружаются, проверки готовности могут вернуть ошибку. При отдельном хосте приложения укажите три HTTPS-адреса внутреннего шлюза и настройки CA; порты GPU-узла по умолчанию опубликованы на loopback для прокси на том же хосте. При изменении `II_MODEL_BIND_IP` ограничьте доступ корпоративной сетью. Входной прокси пропускает GET /health без токена и POST только на заданный для соответствующей роли путь. Потоковые Chat Completions и JSON Decisions проверяют Bearer-токен на сервере модели. Далее запустите [проверку ролей и приёмку приложения](../../docs/backend/local-models.md). Для отката сохраните предыдущие Compose, файл окружения, ревизии весов, сертификаты и образы. Верните этот набор и выполните `gpu_compose up -d`.

## Подтверждение на оборудовании Softline

Запишите SHA репозитория, digest всех образов, фактические модели и устройства, драйвер, длину контекста, число слотов, p50/p95 и пиковую VRAM. Бюджет 128 ГБ при 200 запросах в сутки — расчёт проекта. В измерение входят веса, KV-кеш, служебная память и одновременная работа ролей. Прогон включает проверку цитат, решений судьи и почтового цикла, а также проверку сетевого трафика при нормальной работе и отказе внутреннего сервиса. [План пилота](../../docs/pilot-softline.md)
