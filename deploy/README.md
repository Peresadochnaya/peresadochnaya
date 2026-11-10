# FastAPI и обновление GTFS в k3s

Один образ запускается как API и как GTFS updater. API обслуживает HTTP, updater
запускается CronJob каждые пять минут. `PLANNER_MODE=dummy` явно возвращает
демонстрационные маршруты; OTP и Яндекс пока не подключены.

## Хранилище

PVC `transit-data` (10 GiB, `local-path`, RWO) монтируется в `/data`. API читает
его read-only, updater пишет `/data/gtfs`. Хранится только последний GTFS и
подготовленные из него таблицы; при успешном обновлении предыдущий набор заменяется.
Временные файлы подготовки не являются историей и удаляются после обновления.
Не удаляйте PVC при обычной выкладке или rollback.
Updatеr использует pod affinity к API, чтобы оба pod запускались на одном узле.
`Recreate` исключает перекрытие API pod при обновлении. Сейчас кластер одноузловой;
при его расширении перенос данных между узлами требует отдельной процедуры.

CronJob: `*/20 * * * *` (каждые 20 минут), `Forbid`, deadline 240 секунд, один повтор. Если проверка
занимает дольше пяти минут, новый запуск пропускается. Обновление идемпотентно;
проверка блокировки в updater дополнительно защищает от ручного Job.
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
Прокси работает на TrueNAS `10.147.8.15:18080`, порт опубликован только на
NetBird IP. Политика NetBird разрешает K3S → GTFS Proxy, TCP 18080.
Логин — `traffic`, пароль хранится только в настройках приложения TrueNAS
и Kubernetes Secret. HTTPS до источника проверяется штатным CA bundle.

Сделать GHCR package публичным, либо заранее настроить `imagePullSecrets` для
API и CronJob. Токен registry не включать в ConfigMap или манифест в Git.

## Выкладка

`.github/workflows/backend.yml`: PR запускает тесты и рендер манифестов; push в
`main` публикует `linux/amd64,linux/arm64` image в GHCR и применяет манифесты через
SSH. `workflow_dispatch` также разрешает сборку/деплой выбранной ветки для первого
развёртывания. В продакшн идут только полные commit SHA, не плавающий `latest`.

Для ручной проверки рендера и первой выкладки:

```bash
# В рабочей копии указать опубликованный commit SHA в api.yaml и gtfs-cronjob.yaml.
kubectl kustomize deploy/k8s
kubectl apply -k deploy/k8s
kubectl -n peresadochnaya rollout status deployment/api --timeout=180s
kubectl -n peresadochnaya create job --from=cronjob/gtfs-refresh gtfs-bootstrap
kubectl -n peresadochnaya logs -f job/gtfs-bootstrap
curl --fail https://peresadochnaya.rjnk.cc/health/ready
curl --fail https://peresadochnaya.rjnk.cc/api/v1/data/status
```

Первый bootstrap Job запускает администратор: deploy Role специально не даёт
прав создавать произвольные Jobs. До первого обновления status показывает
отсутствие данных; в dummy режиме это не мешает API принимать демонстрационные
запросы. `active_service_ids=0` — непригодное для текущей даты расписание,
не успех реального планировщика.

## Rollback

Вернуть оба image (API и CronJob) на предыдущий известный commit SHA и повторно
применить манифесты. PVC остаётся на месте. `kubectl rollout undo` откатывает
только Deployment и не меняет CronJob, поэтому для полного отката нужны оба image.
История GTFS не хранится: откат image не возвращает предыдущий ZIP или таблицы.
При ошибке получения/проверки нового набора updater оставляет последний успешно
сохранённый набор на месте.
