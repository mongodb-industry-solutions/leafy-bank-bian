# Contexts — one folder per BIAN Service Domain

Each folder is a bounded context named after a **verified** BIAN v14 Service Domain. The
nine-stage sequence is **not** encoded here — it lives only in
[`../process/payment_lifecycle.py`](../process/payment_lifecycle.py). Read that first.

Per-stage detail (what runs today, what "comprehensive" means, and the TODO list drawn from
Doina's Key Features) is in each module's **docstring**, next to the code it describes.

| Context | SD | id | Doina stage | Today |
|---|---|---|---|---|
| `payment_order_initiation/` | PaymentOrderInitiation | 42933 | 1, 3 | capture, validate, persist — real |
| `party_authentication/` | PartyAuthentication + CustomerAccessEntitlement | 38917, 43057 | 2 | ownership check |
| `payment_orchestration/` | PaymentOrchestration | 48782 | 4a | stub |
| `fraud_evaluation/` | FraudEvaluation | 44625 | 4b | hardcoded score |
| `payment_rail/` | PaymentRail | 47741 | 5 | the ACID money move — real |
| `payment_settlement/` | PaymentSettlement + InternalBankAccount | 40033, 29306 | 7 | write lives in stage 5 |
| `account_reconciliation/` | AccountReconciliation | 35449 | 8 | stub |

Not here, deliberately:

- **Stage 6 (Accounting)** — `FinancialAccounting` (39161) is the **ledger service**, driven by
  a change stream on `transactions`. The payment path writes no ledger data.
- **Stage 9 (Exceptions)** — no BIAN Service Domain exists. Compensation is a saga concern:
  [`../process/compensation.py`](../process/compensation.py).

## Rules

1. Every stage is `run(ctx: PaymentContext) -> None`.
2. **Stages never call each other.** Only the saga sequences them.
3. Each module declares what it reads and writes on `ctx`, at the top.
4. `domain/` imports nothing from `adapters/`, `api/`, or infrastructure.
5. A context earns `domain/`, `ports/`, `adapters/` folders when it has the code to fill
   them — not before. Only `payment_order_initiation/` has them today.

Verify any new Service Domain name with `kg <name> -s bian-ls` before creating a folder.
`PaymentOrderProcedure`, `PaymentInitiation`, and `PaymentExecution` do **not** exist in v14.
