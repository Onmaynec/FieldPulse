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
