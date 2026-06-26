# Leafy Bank — Core Banking (monorepo)

One repository, four deployable services. Consolidates the previously
separate frontend and three backend repos. Each service still ships as
its **own pod** on Kanopy — the monorepo only unifies source and CI.

## Layout

```
leaf-bank-core-banking/
├── frontend/                  # Next.js app (was open-finance-next-gen-ui)
├── backend/
│   ├── accounts/              # FastAPI — accounts service
│   ├── transactions/          # FastAPI — payments/transfers
│   └── ledger/                # FastAPI — ledger service
├── Dockerfile.frontend        # build contexts are the REPO ROOT
├── Dockerfile.accounts
├── Dockerfile.transactions
├── Dockerfile.ledger
├── environment/               # Helm values, one folder per service
│   ├── frontend/{staging,production}.yaml
│   ├── accounts/{staging,prod}.yaml
│   ├── transactions/{staging,prod}.yaml
│   └── ledger/{staging}.yaml          # no prod yet
├── .drone.yml                 # one pipeline file → builds 4 images, deploys 4 releases
├── docker-compose.yml         # local dev: all 4 containers
└── makefile
```

## Services & deploy targets

| Service       | ECR repo (staging / prod)                                   | Helm release                    | Ingress host (staging)                                            |
|---------------|-------------------------------------------------------------|---------------------------------|------------------------------------------------------------------|
| frontend      | `open-finance-nextgen-ui` / `open-finance-nextgen-ui-prod`  | `open-finance-nextgen-ui`       | `open-finance-nextgen-ui.industrysolutions.staging.corp.mongodb.com`     |
| accounts      | `leafy-bank-backend-accounts`                               | `leafy-bank-backend-accounts`   | `leafy-bank-backend-accounts.industrysolutions.staging.corp.mongodb.com` |
| transactions  | `leafy-bank-backend-transactions`                           | `leafy-bank-backend-transactions` | `leafy-bank-backend-transactions.industrysolutions.staging.corp.mongodb.com` |
| ledger        | `leafy-bank-backend-ledger`                                 | `leafy-bank-backend-ledger`     | `leafy-bank-backend-ledger.industrysolutions.staging.corp.mongodb.com`   |

Names, ECR repos, hosts, chart versions and resources are preserved
exactly from the original repos — existing pods/DNS keep working.

> **Ledger has no production deploy yet.** Its prod publish/deploy steps
> in `.drone.yml` are commented out (mirrors the original repo). To enable:
> uncomment both blocks and add `environment/ledger/prod.yaml`.

## CI/CD (Drone + Kanopy)

`.drone.yml` has three pipelines:

- **validate-pr** — on PRs: gitleaks, trivy, frontend lint, and a
  `no_push` build-check of all four Dockerfiles.
- **staging** — on push to `staging`: builds all 4 images, deploys all 4
  Helm releases to the staging cluster.
- **production** — on push to `main`: same for prod (ledger excluded).

Every push rebuilds and redeploys **all four** services. Branch triggers
are strict: `staging` → staging cluster, `main` → production.

Drone secrets (`ecr_access_key`, `ecr_secret_key`, `staging_kubernetes_token`,
`prod_kubernetes_token`, `production_kubernetes_token`) must be set in the
Drone web UI for **this** repo.

## Local development

Everything at once:

```bash
make up        # build + run all 4 containers
make logs      # tail logs
make down      # stop & remove
```

Ports: frontend `8080`, accounts `8001`, transactions `8002`, ledger `8003`.

Single service (no Docker):

```bash
make setup           # poetry install (3 backends) + npm install (frontend)
make dev-accounts    # uvicorn --reload on :8001
make dev-frontend    # next dev
make check           # verify each backend app imports
```

## Secrets

- Local: each service reads its own git-ignored `.env` / `.env.local`.
- Images: `.env` files are excluded via `.dockerignore` — never baked in.
- Kanopy: runtime secrets (e.g. `MONGODB_URI`) resolve via `ksec`,
  referenced as `envSecrets` in the `environment/<service>/*.yaml` files.

**Never commit `.env` files.** `.gitignore` covers them.
