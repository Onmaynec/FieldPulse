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
