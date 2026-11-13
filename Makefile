.PHONY: dev seed test lint
dev:
	docker compose up -d --build
seed:
	docker compose run --rm seed
test:
	pytest -q
lint:
	ruff check app tests
