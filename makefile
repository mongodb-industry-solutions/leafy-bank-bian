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
dev-accounts:
	cd backend/accounts && poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8001

dev-transactions:
	cd backend/transactions && poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8002

dev-ledger:
	cd backend/ledger && poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8003

dev-frontend:
	cd frontend && npm run dev

# Standalone BIAN Data Model Explorer (separate Next.js app). Port 8004 matches
# docker-compose and the frontend NavBar "Data Model" link default.
dev-bian-model:
	cd bian-model && npm run dev -- -p 8004

kill-ports:
	@for port in 3000 8001 8002 8003 8004; do \
		pids=$$(lsof -ti :$$port 2>/dev/null); \
		if [ -n "$$pids" ]; then \
			echo "Killing process on port $$port (PID $$pids)"; \
			kill -9 $$pids 2>/dev/null || true; \
		fi; \
	done

dev: kill-ports
	trap 'kill 0' INT; \
	(cd backend/accounts && poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8001) & \
	(cd backend/transactions && poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8002) & \
	(cd backend/ledger && poetry run uvicorn main:app --reload --host 0.0.0.0 --port 8003) & \
	(cd frontend && npm run dev) & \
	(cd bian-model && npm run dev -- -p 8004) & \
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

install-bian-model:
	cd bian-model && npm install --no-audit --legacy-peer-deps

setup: install-accounts install-transactions install-ledger install-frontend install-bian-model

# ---------- Sanity: do the backend apps import? ----------
check:
	cd backend/accounts && poetry run python -c "from main import app; print('OK accounts')"
	cd backend/transactions && poetry run python -c "from main import app; print('OK transactions')"
	cd backend/ledger && poetry run python -c "from main import app; print('OK ledger')"

.PHONY: build up start stop down logs clean \
	kill-ports dev dev-accounts dev-transactions dev-ledger dev-frontend dev-bian-model \
	install-accounts install-transactions install-ledger install-frontend install-bian-model setup check
