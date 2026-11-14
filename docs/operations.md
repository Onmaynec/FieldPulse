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
