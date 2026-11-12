# FastAPI и обновление GTFS в k3s

Один образ запускается как API и как GTFS updater. API обслуживает HTTP, updater
запускается CronJob каждые 20 минут. Отдельный `metro-refresh` с той же частотой
строит оценочный GTFS метро из официальных интервалов и времён pMetro. `PLANNER_MODE=dummy` явно возвращает
демонстрационные маршруты; OTP и Яндекс пока не подключены.

## Хранилище

PVC `transit-data` (10 GiB, `local-path`, RWO) монтируется в `/data`. API читает
его read-only, updater пишет `/data/gtfs`, updater метро — `/data/metro`. Хранится только последний GTFS и
подготовленные из него таблицы; при успешном обновлении предыдущий набор заменяется.
Временные файлы подготовки не являются историей и удаляются после обновления.
Не удаляйте PVC при обычной выкладке или rollback.
Updatеr использует pod affinity к API, чтобы оба pod запускались на одном узле.
`Recreate` исключает перекрытие API pod при обновлении. Сейчас кластер одноузловой;
при его расширении перенос данных между узлами требует отдельной процедуры.

CronJob: `*/20 * * * *` (каждые 20 минут), `Forbid`, deadline 240 секунд, один повтор. Если проверка
занимает дольше 20 минут, новый запуск пропускается. Обновление идемпотентно;
проверка блокировки в updater дополнительно защищает от ручного Job.
Для каждого refresh хранится не больше одного успешного и одного упавшего Job.
TTL завершённых Job — 1 час; Kubernetes удаляет Job вместе с его подами.
Код cronjob и API выкатывается одним commit SHA. GTFS не включается в Docker-образ
и хранится независимо от его версии.

## Однократная подготовка администратором

Проверить Traefik certificate resolver: манифест предполагает имя `letsencrypt`.
Если в кластере другое имя, заменить аннотацию в `k8s/ingress.yaml`.
Проверить DNS `peresadochnaya.rjnk.cc` и наличие `local-path` StorageClass.

```bash
kubectl apply -f deploy/k8s/namespace.yaml
kubectl apply -f deploy/bootstrap-rbac.yaml
```

Создать отдельного SSH deploy-пользователя. Его `kubectl` использует kubeconfig
service account `github-deployer`, ограниченного namespace `peresadochnaya`.
Пользователь называется `peresadochnaya`, не имеет sudo. Во всех deploy-командах
**явно** задаётся `KUBECONFIG=/home/peresadochnaya/.kube/config`: окружение k3s
может иначе выбрать административный kubeconfig.
Не передавать ему `/etc/rancher/k3s/k3s.yaml` администратора. Настроить обновление
ограниченного токена kubeconfig до истечения срока. Root-owned systemd timer
`peresadochnaya-kubeconfig.timer` ежедневно выпускает токен на 48 часов через
TokenRequest и атомарно обновляет root-owned файл
`/var/lib/peresadochnaya-deployer/config` (0640, группа `peresadochnaya`).
`~/.kube/config` — ссылка на него; таймер не пишет через пользовательскую папку.
Таймер работает с
административным kubeconfig, который deploy-пользователю недоступен. Повторная
подготовка: `sudo bash deploy/bootstrap-deployer.sh` на узле; ручное продление:
`sudo systemctl start peresadochnaya-kubeconfig.service`. Скрипты не печатают токен.
SSH ключ ограничить отдельным
пользователем и запретить forwarding (`restrict` в `authorized_keys`).

В GitHub создать environment `production`, при необходимости с ручным gate.
Secrets (значения в Git не добавлять):

| Имя | Назначение |
|---|---|
| `DEPLOY_HOST` | NetBird IP k3s: `10.147.8.210` |
| `DEPLOY_USER` | отдельный deploy-пользователь |
| `DEPLOY_PORT` | SSH порт, по умолчанию 22 |
| `DEPLOY_SSH_KEY` | приватный deploy-ключ |
| `DEPLOY_KNOWN_HOSTS` | проверенный host key узла, не результат слепого trust-on-first-use |
| `NB_SETUP_KEY` | ephemeral reusable setup key, автоматически включает runner только в deploy-группу |
| `NB_MANAGEMENT_URL` | self-hosted management URL: `https://lab.rjnk.cc` |

Runner устанавливает официальный CLI с `pkgs.netbird.io`, подключается через
`netbird up --setup-key-file <0600 temporary file> --management-url ...` и после
deploy всегда выполняет `netbird down`. Приватный setup key не передаётся в
аргументах процесса и не печатается. Ephemeral peers должны удаляться менеджментом
после отключения. Для CI-группы разрешена односторонняя политика `netbird-ssh`
на порт 22 группы K3S. Используется уже работающий NetBird SSH; дополнительный
sshd или порт не нужен. Команды выполняются Linux-пользователем `peresadochnaya`.
`DEPLOY_KNOWN_HOSTS` содержит публичный ключ NetBird SSH для `10.147.8.210`,
проверенный через существующий доверенный административный канал.
При включённом на сервере `--disable-ssh-auth` доступ контролирует политика
NetBird, а не `authorized_keys`: указанный в workflow пользователь сам по себе
не запрещает разрешённому peer выбирать других локальных пользователей.

Environment variable `API_BASE_URL` по умолчанию `https://peresadochnaya.rjnk.cc`.
GTFS updater получает `HTTPS_PROXY` из Secret `gtfs-proxy`, ключ `url`.
Secret создаётся отдельно от Git; его отсутствие допускает прямое подключение.
Прокси работает на роутере arctur `10.147.8.92:18080`, слушает только
NetBird IP. Политика NetBird `K3S to Arctur HTTP proxy` разрешает K3S →
Arctur HTTP Proxy, TCP 18080. Это замена недоступного TrueNAS.
Логин — `traffic`, прежний пароль сохранён в Kubernetes Secret и закрытом
`/etc/traffic-proxy/config.json` на роутере (0600, каталог 0700).
HTTPS до источника проверяется штатным CA bundle.

На arctur используется установленный sing-box отдельным процессом, без Docker
и без изменений основного sing-box/Podkop. Сервис `/etc/init.d/traffic-proxy`
управляется procd, включён в `/etc/rc.d/S99traffic-proxy` и использует
`respawn 3600 5 0` (перезапуск через 5 секунд без ограничения числа попыток).
Обёртка `/etc/traffic-proxy/run.sh` перед запуском ждёт появления адреса
`10.147.8.92` на `wt0`. Автозапуск включён, восстановление процесса после
остановки проверено; весь роутер для проверки не перезагружался.
Остановить только этот прокси:
`ssh arctur '/etc/init.d/traffic-proxy stop; /etc/init.d/traffic-proxy disable'`.

Через этот Secret ходят GTFS updater и metro updater. Яндекс из backend продолжает
вызываться напрямую с k3s (`trust_env=False`); его маршрут не изменён.

Сделать GHCR package публичным, либо заранее настроить `imagePullSecrets` для
API и CronJob. Токен registry не включать в ConfigMap или манифест в Git.

## Выкладка

`.github/workflows/backend.yml`: PR запускает тесты и рендер манифестов; push в
`main` публикует `linux/amd64,linux/arm64` image в GHCR и применяет манифесты через
SSH. `workflow_dispatch` также разрешает сборку/деплой выбранной ветки для первого
развёртывания. В продакшн идут только полные commit SHA, не плавающий `latest`.

Для ручной проверки рендера и первой выкладки:

```bash
# В рабочей копии указать опубликованный commit SHA в api.yaml, gtfs-cronjob.yaml и metro-cronjob.yaml.
kubectl kustomize deploy/k8s
kubectl apply -k deploy/k8s
kubectl -n peresadochnaya rollout status deployment/api --timeout=180s
kubectl -n peresadochnaya create job --from=cronjob/gtfs-refresh gtfs-bootstrap
kubectl -n peresadochnaya logs -f job/gtfs-bootstrap
kubectl -n peresadochnaya create job --from=cronjob/metro-refresh metro-bootstrap
kubectl -n peresadochnaya logs -f job/metro-bootstrap
curl --fail https://peresadochnaya.rjnk.cc/health/ready
curl --fail https://peresadochnaya.rjnk.cc/api/v1/data/status
```

Первый bootstrap Job запускает администратор: deploy Role специально не даёт
прав создавать произвольные Jobs. До первого обновления status показывает
отсутствие данных; в dummy режиме это не мешает API принимать демонстрационные
запросы. `active_service_ids=0` — непригодное для текущей даты расписание,
не успех реального планировщика.

## Rollback

Вернуть все image (API и оба CronJob) на предыдущий известный commit SHA и повторно
применить манифесты. PVC остаётся на месте. `kubectl rollout undo` откатывает
только Deployment и не меняет CronJob, поэтому для полного отката нужны image обоих CronJob.
История GTFS не хранится: откат image не возвращает предыдущий ZIP или таблицы.
При ошибке получения/проверки нового набора updater оставляет последний успешно
сохранённый набор на месте.


## OTP в k3s

OTP запускается отдельным Deployment `otp` и закрытым `ClusterIP` на порту 8080.
Ingress, публичный DNS и NodePort ему не нужны: фронт обращается к backend
`POST /api/v1/routes?dummy=false`. Backend настроен через `PLANNER_MODE=otp` и
`OTP_URL=http://otp:8080`. `dummy=true` остаётся демо по умолчанию.

Один закреплённый digest OTP используется сервером и сборщиком. Граф лежит на
том же PVC в `/data/otp/versions/<version>`; `/data/otp/current` выбирает активный
каталог, `current.json` хранит происхождение данных. Сервер получает его read-only.
После смены указателя нужен restart OTP, backend перезапускать для графа не надо.

Карта OSM помещается в `/data/otp/osm/spb-lo.osm.pbf` на PVC. В локальном модуле
ожидаемый путь — `otp/data/osm/spb-lo.osm.pbf`; PBF не включается в Git/образ.
Пересборка запускается явно оператором с административным kubectl-контекстом:

```bash
./otp/scripts/rebuild-k3s.sh
```

Скрипт берёт helper image из работающего Deployment API и создаёт отдельный Job
с подготовкой входов, сборкой и публикацией. В нём используются наземный GTFS
из `/data/gtfs` и оценочный метро GTFS из `/data/metro`. Инвертированные даты
календаря исправляются только в staging-копии; это не подтверждение официальной
доступности расписания. Оба source SHA входят в версию графа. Ошибка до публикации
не меняет текущий граф; неизменная версия пропускает сборку и restart. После
успешного обновления скрипт перезапускает только OTP.

Это пока ручной запуск: периодические refresh GTFS сами граф не пересобирают.
`otp-build-job.yaml` намеренно не включён в kustomization, чтобы обычный деплой
backend не запускал тяжёлую сборку. Скрипт требует права на Jobs и restart OTP;
GitHub deploy Role эти дополнительные права не получает.

Builder: heap 6G, memory limit 8Gi; server: heap 4G, memory limit 6Gi. На текущем
двухъядерном узле сборка заняла около трёх минут, сервер использует около 2.7Gi.
Перед параллельной пересборкой нужно учитывать текущую свободную память.

Отладочный GraphiQL доступен через port-forward, без публикации OTP в интернет:

```bash
kubectl -n peresadochnaya port-forward svc/otp 8080:8080
# http://localhost:8080/graphiql
```

Реальная интеграция пока принимает только `providers=[otp]`; фильтры modes и
exclude_modes передаются в OTP. Не-дефолтные max_transfers, walk_speed и
less_walking пока отклоняются явно, стоимость проездных и дорожные поправки
не рассчитываются. Ответ содержит provenance/sources и предупреждения о
модельных временах метро и исправлении календаря.


## Веб-интерфейс

Production-сборка React/Vite включена в образ API через Node build stage.
Фронт доступен в корне https://peresadochnaya.rjnk.cc/, API — на /api/v1,
Swagger — на /docs. Отдельный домен и CORS для этого не нужны.
CI запускает тесты и сборку фронта перед публикацией образа.

`/map-api.js` перенаправляет браузер на JavaScript API Яндекса с отдельным
ключом из Secret `yandex-api/js-api` (env `YANDEX_JS_API_KEY`). Ключ JS API
доступен браузеру по назначению; серверные ключи Router/Geocoder не выдаются.
При ротации ключа достаточно обновить Secret и перезапустить Deployment api;
пересобирать фронт не требуется. `dummy=true` остаётся по умолчанию.


## Фильтрация повторных отправлений OTP

В `otp/config/router-config.json` включён встроенный фильтр
`routingDefaults.itineraryFilters.removeItinerariesWithSameRoutesAndStops`.
OTP оставляет первый вариант с одинаковыми линиями и остановками.
Бэкенд не объединяет результаты самостоятельно.

При старте pod initContainer накладывает только `itineraryFilters` из
задеплоенного образа на router-config активного графа в `emptyDir`.
Идентификатор версии, прочие настройки и файлы графа сохраняются;
полученный router-config монтируется отдельным файлом поверх каталога графа.
Для этого изменения пересборка graph.obj не нужна, нужен перезапуск OTP,
который делает обычный деплой нового образа initContainer.
CI ожидает готовности API и OTP.


## Пригородные электрички

В образ включён готовый снимок `data/processed/rail/feed.zip` (до 18 ноября
2026 включительно). Подготовка графа устанавливает его в `/data/rail` только
при отсутствии текущего снимка. Новая полученная версия не заменяется
более старой, в том числе при откате образа. При подготовке OTP добавляется
`rail.gtfs.zip`, SHA и период записываются в метаданные графа.
Календарь электричек не продлевается и не проходит через исправление
наземного календаря. Для начала работы нужен ручной rebuild графа:
`otp/scripts/rebuild-k3s.sh`. Сам деплой API граф не пересобирает.

Подготовлен `rail-refresh`, ежедневный в 03:00 по Москве, но `suspend=true`.
Для включения нужен Secret `yandex-rasp/apikey` с отдельным ключом API
Расписаний и согласованный бюджет: сбор недели выполняет много запросов.
Ручной запуск загрузчика: `python -m data.rail.refresh --store-dir /data/rail
--raw-dir /data/rail-raw --days 7`. После обновления нужен rebuild графа.
Данные ограничены рейсами, найденными по выбранным вокзалам, а не всей сетью.

После ручной сборки скрипт также запускает одноразовую `otp-verify-*` Job:
она проверяет загруженную версию и фиксирует `activated_at` в метаданных.
Без успешной проверки новая версия не считается подтверждённо загруженной.
Электрички доступны в существующих `/stops`, `/transit-routes`, `/trips` и
`/stops/{stop_id}/departures` по собственным `rail:*` ID; demo не меняется.
