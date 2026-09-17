.PHONY: help install run seed test check-frontend smoke compose-up compose-down clean

help:
	@echo "install        - install runtime + dev dependencies"
	@echo "run            - start the application (http://localhost:8000)"
	@echo "seed           - load demo accounts, orders and the FAQ knowledge base"
	@echo "test           - run the test suite"
	@echo "check-frontend - verify frontend element ids / asset references (needs node)"
	@echo "smoke          - run the end-to-end smoke test against a live server"
	@echo "compose-up     - start the full stack with docker compose"
	@echo "compose-down   - stop the docker compose stack"

install:
	python -m pip install -r requirements-dev.txt

run:
	python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload

seed:
	python scripts/seed.py

test:
	python -m pytest

check-frontend:
	node scripts/check_frontend.mjs

smoke:
	python scripts/smoke_test.py --base-url http://127.0.0.1:8000

compose-up:
	docker compose up -d --build

compose-down:
	docker compose down

clean:
	python -c "import shutil,pathlib;[shutil.rmtree(p,ignore_errors=True) for p in pathlib.Path('.').rglob('__pycache__')]"
