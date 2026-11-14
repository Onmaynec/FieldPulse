# Эксплуатация и восстановление

Проверка контейнеров: docker compose ps. Логи: docker compose logs -f api worker. Health: /health/live и /health/ready. API записывает JSON-логи с correlation ID, маршрутом, кодом ответа и задержкой.

Миграции: docker compose run --rm migrate. Для следующей версии добавить отдельную Alembic-ревизию. Наполнение: docker compose run --rm seed (не пересоздаёт существующие данные).

## Резервная копия PostgreSQL

Команда сохранения: docker compose exec -T postgres pg_dump -U fieldpulse -Fc fieldpulse > fieldpulse.dump

Для восстановления на отдельном пустом экземпляре: docker compose exec -T postgres pg_restore -U fieldpulse -d fieldpulse --clean --if-exists < fieldpulse.dump

Фотографии сохраняются в томе minio_data; для полного восстановления отдельно нужна согласованная копия этого тома. Только SQL-дамп **не содержит** фотографий.

## Диагностика

Если API не видит базу — проверить postgres и DATABASE_URL. При ошибке 503 фото — проверить MinIO и S3_* параметры. При задержке SLA-событий — статус worker, таблицы events и outbox. Если синхронизация вернула конфликт, данные остаются в IndexedDB и должны быть разобраны вручную.

Демо-инфраструктура не заменяет продакшен-мониторинг, регулярную проверку восстановления и централизованное управление секретами.


## Обновление до v0.2.0

1. Скопировать базу PostgreSQL и S3-том.
2. Выполнить docker compose up -d --build: сервис migrate применяет 0002 до запуска API.
3. Проверить /health/ready и логи worker.
4. На **тестовой** базе выполнить docker compose exec -T api python scripts/smoke.py (скрипт создаёт тестовую заявку, два фото и редакции акта).

На рабочей базе не использовать docker compose down -v: этот параметр удаляет именованные тома.

## v0.3.0 — резервное копирование и проверка

Создание и проверка полного архива (внутри API-контейнера):

```bash
docker compose exec -T api python -m app.backup create /tmp/fieldpulse-backup.zip
docker compose exec -T api python -m app.backup verify /tmp/fieldpulse-backup.zip
docker cp $(docker compose ps -q api):/tmp/fieldpulse-backup.zip ./fieldpulse-backup.zip
```

Архив содержит PostgreSQL custom dump и объекты S3; SHA-256 проверяется для каждой записи. Одновременная работа API/worker означает, что между БД и S3 не гарантируется единая точка времени. Для согласованного бэкапа используйте окно обслуживания.

Восстановление допускается **только в новой пустой среде** после подготовки схемы. Остановите API и worker, проверьте архив и смонтируйте его в отдельный контейнер приложения:

```bash
docker compose run --rm -v /absolute/backup-directory:/backups:ro api python -m app.backup restore /backups/fieldpulse-backup.zip --confirm-empty
```

Скрипт не восстановит данные поверх непустой базы или S3-бакета. Загрузка БД и S3 неатомарна; при сбое восстановление необходимо повторить с новой пустой среды. Сквозное восстановление в CI пока не подтверждено.

Проверка осиротевших фото (без удаления):

```bash
docker compose exec -T api python -m app.maintenance --grace-hours 24 --limit 100
```

Удаление требует флагов --delete --confirm и минимум 24 часа выдержки.

Нагрузочный профиль выполняйте **только на демонстрационной базе**, поскольку он создаёт реальные записи:

```bash
docker compose exec -T api python scripts/load_profile.py --tickets 200 --photos-per-ticket 5 --workers 8 --allow-writes
```

JSON-результат содержит задержки p50/p95, ошибки и параметры стенда. Два задания в CI — только smoke, а не доказательство целевой нагрузки.

## v0.4.0 — проверка визуальных сценариев

В CI после развёртывания демонстрационной базы выполняется `python scripts/visual_e2e.py` с Playwright Chromium и генерируются PNG в `visual-artifacts/`. Они загружаются в артефакт `fieldpulse-v04-ui-screenshots`. Сценарий проверяет вход для пяти ролей, реестр с фильтрами, светлую/тёмную темы, плотность таблиц, мобильное меню и отсутствие горизонтального переполнения на 390 px. Автоснимки являются контрольным материалом для ручного просмотра; пиксельные сравнения с утверждённым эталоном пока не настроены.

Сервис-воркер обновлён до `fieldpulse-shell-v4`; при первом открытии после деплоя необходима загрузка приложения онлайн для кэширования новой версии. Структура офлайн-очереди не меняется, устаревшие локальные рабочие записи не удаляются только по причине обновления темы.


## v0.5.0 — запуск, диагностика и тесты

Создайте проверенную резервную копию PostgreSQL/S3, затем запустите `docker compose up -d --build`. Контейнер `migrate` должен применить `0003_ticket_views` до старта API. После старта проверьте `/health/ready` и выполнение браузерного реестра под каждой необходимой ролью.

Проверяемые маршруты:

- `GET /api/v1/tickets/search?page=1&page_size=20&sort=deadline` — доступная пользователю выборка.
- `GET /api/v1/tickets/views` — список только собственных сохранённых представлений.
- `GET /api/v1/reports/operations` — доступен персоналу, содержит `open_p1_tickets`, `unassigned_open_tickets`, `last_event_at` и `oldest_pending_mock_age_seconds`.

Проверка: `pytest -q tests/test_v05.py` и `python scripts/registry_e2e.py` при запущенном демонстрационном Compose. В CI выполняются также старые offline и visual E2E.

`oldest_pending_mock_age_seconds` — не healthcheck worker, а время с момента самой старой записи, не помеченной отправленной. Никакого внешнего канала доставки сейчас не настроено. Service Worker `fieldpulse-shell-v5` обновляет оболочку при первом онлайн-запуске.


## v0.6.0 — устранение отказа SLA worker

1. До обновления создайте и проверьте резервную копию PostgreSQL и медиа. Запустите `docker compose up -d --build`, дождитесь миграции `0004_worker_observability` и доступности API.
2. Проверьте `docker compose ps` и `docker compose exec -T worker python -m app.worker_health --check`. Код выхода 0 означает зарегистрированный свежий успешный проход, но не отправку внешних уведомлений.
3. Диспетчер, администратор и руководитель видят `GET /api/v1/reports/worker-health` или карточку worker в аналитике. Поля: `state`, `seconds_since_success`, `last_success_at`, `last_failure_at`, `last_duration_ms`, `last_error_type`, `cycles`, `failures`.
4. Если `error` — проверьте `docker compose logs --tail 200 worker` и PostgreSQL. Если `stale` (более **75 секунд** без успешного прохода), проверьте блокировки/CPU/подключение к БД. Если `unknown` — проверьте, начал ли процесс выполнять цикл. После исправления подтверждайте движение `last_success_at` и отсутствие дублей SLA-событий.
5. Внешнее оповещение о состоянии worker и доставка mock inbox пока не настроены: рост очереди не является подтверждением работы или остановки сканера.

## v0.6.0 — PostgreSQL планы запросов

На тестовом Compose запустите `docker compose exec -T api python -m scripts.search_plan --repeats 7 --expected-min 2000 --output /tmp/fieldpulse-search-plan.json`. Скрипт измеряет SQL EXPLAIN ANALYZE, буферы и p50/p95 времени запросов без записи новых заявок. CI сохраняет JSON в артефакт `fieldpulse-v06-postgres-plans`.

Опционально на отдельном тестовом PostgreSQL: `--generate 50000 --allow-demo-write` или `--generate 200000 --allow-demo-write`. Новые заявки находятся в транзакции и откатываются; ресурсная нагрузка настоящая, не запускать на продуктивной системе.

Курсор `GET /api/v1/tickets/seek?page_size=20&sort=deadline` выдаёт `next_cursor` для следующего шага. Он действует максимум час, и его нельзя переносить между пользователями и фильтрами. Нумерованные страницы остались в `/tickets/search`.


## v0.7.0 — инциденты SLA и уведомления

После проверки резервной копии выполните `docker compose up -d --build`. Ревизия `0005_operational_incidents` добавит таблицы `operational_incidents`, `incident_deliveries` и `monitor_cycles`. Новый сервис `monitor` запускается независимо от `worker`.

Проверки работоспособности:

    docker compose ps
    docker compose exec -T worker python -m app.worker_health --check
    docker compose exec -T monitor python -m app.monitor --check
    docker compose logs --tail 100 monitor worker

Интервал monitor — 15 секунд. Ошибка worker фиксируется сразу, устаревшее подтверждение — после 75 секунд, отсутствие первоначального успеха — после 90 секунд от запуска наблюдения. В аналитике откройте «Инциденты и оповещения»; персонал может отметить инцидент просмотренным. Это не отключает предупреждения, не скрывает неисправность и не прерывает отслеживание.

По умолчанию `ALERT_WEBHOOK_URL` пустой — внешний вызов не выполняется. При наличии оператора HTTPS-получателя добавьте в локальный `.env`:

    ALERT_WEBHOOK_URL=https://alerts.example.net/fieldpulse

Не сохраняйте действительный URL с токеном в Git, документации или логах. Запускайте `docker compose up -d --build` повторно, чтобы сервис получил переменную окружения. Доставка JSON имеет типы `fieldpulse.worker.opened` и `fieldpulse.worker.recovered`, стабильное поле `idempotency_key` и только обезличенные метаданные. Redirect запрещён, HTTP и буквальные приватные IP отклоняются. При сбое доставки попытки повторяются с backoff до 900 секунд. Внешний endpoint должен безопасно дедуплицировать ключи — **система обеспечивает at-least-once, а не exactly-once**.

Если инцидент возник, проверьте обе команды healthcheck, `docker compose logs worker monitor`, подключение к PostgreSQL и причину error/stale. После восстановления проверьте `recovered_at` и отсутствие дублей SLA Event. Если PostgreSQL отключён, monitor тоже не может записать инцидент и отправить новые уведомления. Нужна отдельная инфраструктурная проверка БД и Docker-хоста.

**Важно:** webhook применяется исключительно к авариям SLA-worker, а не ко всем письмам из `outbox`. Внешняя доставка уведомлений по заявкам не появилась.

## v0.7.0 — масштабные измерения

Отдельное CI-задание `large-search-benchmark` собирает планы PostgreSQL с 50 000 и 200 000 синтетических строк сверх seed в независимом временном Compose, по три повторения для сортировок и текстового поиска. Перед и после измерений подтверждается, что строки профиля не остались в БД. Артефакт `fieldpulse-v07-query-scale` содержит оба JSON, но только если задание завершилось с фактическими данными.

На рабочей БД скрипты генерации **не запускать**, даже если записи после прогона откатываются: блокировки, WAL, CPU, RAM и обновление статистики планировщика реальны. Протокол и ограничения — `docs/benchmarks/v0.7.0-query-scale.md`.


В Compose v0.7.0 для сервисов `monitor` и `worker` указана политика `restart: unless-stopped`. Она помогает восстановиться после завершения процесса, но **не** заменяет разбор причины инцидента. Для экспериментов и обновления штатно используйте `docker compose stop` / `docker compose restart`, а не удаление томов.
