# 0004. Публічний стенд живе на k3s, а не на devhub

## Status

Accepted, 2026-10-11. Issue #32.

## Context

Публічний стенд (paypilot.xmesh.pl, paypilot-traces.xmesh.pl) працював на devhub:
`docker compose` плюс `cloudflared-paypilot`. devhub вимикається на ніч і
переїжджає на окремі машини, а стенд із ключами студентів (ADR 0002) має бути
доступним завжди.

Обмеження з коду:

- Ліміт на ключ тримається в пам'яті одного процесу (ADR 0002, Consequences), а
  `data/budget.db`, `data/paypilot.db` і `data/tenants` — SQLite на диску. Дві
  репліки або «нова поряд зі старою» пошкодять облік.
- Ключі студентів живуть лише в `budget.db` тому `stand-data`.
- Токена Cloudflare для зони xmesh.pl немає, а записи трьох хостів зараз —
  CNAME на тунель `33ee488e-9cb8-4213-81f9-6e4647cc162b`.

## Decision

1. Чарт `install/helm`, реліз і namespace `paypilot-stand`, деплой — Jenkins
   (`Jenkinsfile`, простий pipeline-job на `main`), який запускає
   `.github/workflows/deploy.yml`; job створює `./scripts/jenkins-job.sh paypilot-stand sergeytkachenko/paypilot-stand-shared`
   (одноразово), а Jenkinsfile сам анотує namespace `ingress.xmesh.pl/allowed-hosts`.
2. Стенд: `replicas: 1` і `strategy: Recreate`. RollingUpdate заборонений, бо
   дві одночасні копії пишуть у ті самі SQLite-файли й мають окремі lock-и.
3. Три PVC (`data`, `traces`, `phoenix`) на `local-path` з анотацією
   `helm.sh/resource-policy: keep`: `helm uninstall` не знищує ключі студентів.
4. Phoenix закріплений на `arizephoenix/phoenix:version-20.15.0-nonroot` (та
   сама версія, що писала існуючу `phoenix.db`; є arm64), без `latest`.
5. Под працює не від root: `runAsUser/fsGroup 65532`. Образ не змінено,
   пише лише в змонтовані `/stand/data` і `/stand/traces`.
6. Публікація — `cloudflared` у кластері з тим самим тунелем: DNS не міняється.
   Конфіг віддає лише paypilot.xmesh.pl і paypilot-traces.xmesh.pl, решта —
   `http_status:404`. Є й Ingress (traefik, як у інших застосунків), тож перехід на
   проксований A-запис не потребує змін чарта. `paypilot-video.xmesh.pl` (порт
   8099) стенду не належить і не маршрутизується.
7. Секрети — два Kubernetes Secret, створені вручну в namespace:
   `paypilot-stand-env` (`ANTHROPIC_API_KEY`, `STAND_ADMIN_TOKEN`,
   `TYPESAFE_API_KEY`) і `paypilot-stand-tunnel` (`credentials.json` тунелю).
   Jenkins лише перевіряє, що вони є. У 1Password і в
   `jenkins-credentials.yaml` вони поки не внесені.
8. Несекретна конфігурація — `values.yaml` (`config`), значення перенесені з
   `.env` devhub.

Конектор був вимкнений (`cloudflared.enabled: false`) на час міграції і ввімкнений
окремою зміною після неї, одночасно з вимкненням конектора devhub: інакше
Cloudflare віддавав би частину запитів порожньому стенду.

Міграція даних: перший деплой, devhub-стенд зупинений (контейнери `Exited`),
потім `kubectl scale --replicas=0` стенду й
phoenix, `tar` томів із devhub у PVC (`budget.db` ніколи не підміняється під
живим процесом), `chown`, `scale --replicas=1`.

## Consequences

- Два конектори одного тунелю балансуються Cloudflare. Доки на devhub працює
  `cloudflared-paypilot`, частина запитів іде на порожні порти devhub (502); його
  треба вимкнути: `systemctl disable --now cloudflared-paypilot`.
- Секрети існують лише в кластері та в `.env` devhub; відновлення з нуля потребує
  їх повторного створення (`kubectl create secret ... --from-env-file`).
- Горизонтальне масштабування неможливе без нового ADR (спільний lock і не SQLite).
- Том `local-path` прив'язаний до вузла, де він створився.
