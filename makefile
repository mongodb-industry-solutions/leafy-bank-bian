####################################################################
# Leafy Bank Core Banking — monorepo makefile
# Frontend + 3 backends (accounts, transactions, ledger).
####################################################################

# ---------- Docker (all services) ----------
build:
	docker compose build

up:
	docker compose up --build -d

start:
	docker compose start

stop:
	docker compose stop

down:
	docker compose down

logs:
	docker compose logs -f

clean:
	docker compose down --rmi all -v

# ---------- Per-service local dev (poetry) ----------
# Each backend runs on its own host port to match docker-compose.
#
# `.venv/bin/python -m uvicorn`, NOT `poetry run uvicorn` and NOT `.venv/bin/uvicorn`.
#
# Both alternatives broke on 2026-08-31 for the same underlying reason: these venvs were
# COPIED from a sibling checkout (repos/leaf-bank-bian), so every console script in
# .venv/bin still carries that checkout's interpreter in its shebang:
#
#   #!/…/repos/leaf-bank-bian/backend/accounts/.venv/bin/python
#
# Running the script therefore execs the OTHER venv's python, picks up ITS site-packages
# (missing anything installed here), and imports main.py from the CWD — a hybrid that
# fails on a dependency which is demonstrably installed. `.venv/bin/python` is a symlink
# to pyenv and is unaffected, which is what makes the fault so hard to see.
#
# `python -m` uses the interpreter directly and ignores every shebang, so a relocated or
# copied venv cannot misdirect it.
UVICORN = .venv/bin/python -m uvicorn

dev-accounts:
	cd backend/accounts && $(UVICORN) main:app --reload --host 0.0.0.0 --port 8001

dev-transactions:
	cd backend/transactions && $(UVICORN) main:app --reload --host 0.0.0.0 --port 8002

dev-ledger:
	cd backend/ledger && $(UVICORN) main:app --reload --host 0.0.0.0 --port 8003

dev-frontend:
	cd frontend && npm run dev

ensure-indexes:
	cd backend/ledger && poetry run python -m data.ensure_indexes

kill-ports:
	@for port in 3000 8001 8002 8003; do \
		pids=$$(lsof -ti :$$port 2>/dev/null); \
		if [ -n "$$pids" ]; then \
			echo "Killing process on port $$port (PID $$pids)"; \
			kill -9 $$pids 2>/dev/null || true; \
		fi; \
	done

dev: kill-ports
	trap 'kill 0' INT; \
	(cd backend/accounts && $(UVICORN) main:app --reload --host 0.0.0.0 --port 8001) & \
	(cd backend/transactions && $(UVICORN) main:app --reload --host 0.0.0.0 --port 8002) & \
	(cd backend/ledger && $(UVICORN) main:app --reload --host 0.0.0.0 --port 8003) & \
	(cd frontend && npm run dev) & \
	wait

# ---------- Per-service poetry setup ----------
install-accounts:
	cd backend/accounts && poetry config virtualenvs.in-project true && poetry install --no-interaction --no-root

install-transactions:
	cd backend/transactions && poetry config virtualenvs.in-project true && poetry install --no-interaction --no-root

install-ledger:
	cd backend/ledger && poetry config virtualenvs.in-project true && poetry install --no-interaction --no-root

install-frontend:
	cd frontend && npm install --no-audit

setup: install-accounts install-transactions install-ledger install-frontend

# ---------- Sanity: do the backend apps import? ----------
check:
	cd backend/accounts && poetry run python -c "from main import app; print('OK accounts')"
	cd backend/transactions && poetry run python -c "from main import app; print('OK transactions')"
	cd backend/ledger && poetry run python -c "from main import app; print('OK ledger')"

.PHONY: build up start stop down logs clean \
	kill-ports dev dev-accounts dev-transactions dev-ledger dev-frontend \
	install-accounts install-transactions install-ledger install-frontend setup check
