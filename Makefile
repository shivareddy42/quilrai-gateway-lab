.PHONY: setup check test demo gateway mocks

setup:
	uv sync --locked

check:
	uv run --locked ruff check src tests
	uv run --locked ruff format --check src tests

test:
	uv run --locked pytest --cov=quilr_gateway --cov-report=term-missing --cov-fail-under=94

demo:
	uv run --locked quilr-demo

gateway:
	QUILR_DEMO=1 uv run --locked uvicorn quilr_gateway.app:create_app --factory --host 127.0.0.1 --port 8000 --no-access-log

mocks:
	uv run --locked uvicorn quilr_gateway.mocks:app --host 127.0.0.1 --port 9001 --no-access-log
