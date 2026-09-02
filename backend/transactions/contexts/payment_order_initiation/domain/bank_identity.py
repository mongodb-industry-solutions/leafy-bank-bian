"""Leafy Bank's own agent identity — one definition, imported everywhere. Doc 17 §3 step 2.

Stamped on whichever side of a payment the bank holds, and read by stage 3 for two things
it cannot do without:

- **Domestic vs cross-border (R9).** Doina L439: *"the system compares the beneficiary
  bank's country code against the originating bank's own country code."* `OUR_BANK_COUNTRY`
  is that second operand. `derive_wire_type()` already performs the comparison; this is
  where the value it compares against lives.
- **Our clearing member id (R14).** `OUR_ABA` is new here. It was missing entirely: party
  snapshots wrote `clearingSystemMemberId: None` for internal parties, so on an outbound
  domestic wire the *debtor* — always us — had no routing number. That is exactly the gap
  D10 named when it said the spec's own `wire_domestic` sample is unsendable without it.

## Why this is not configuration

Hardcoded, deliberately. B6 rejected env vars: a bank's own BIC is not deployment-varying
in this demo, and it would add a configuration site coupled to nothing (working-discipline:
no flexibility that wasn't requested). B6 also rejected resolving our identity from the
seeded directory — that would make stage 1 document construction depend on a reference-data
round-trip, coupling the money path to a lookup for a constant.

The directory *also* holds a `LEAFUS33` row, and the two must agree. They are asserted equal
in `test_reference_data.py` rather than one deriving from the other, so a mismatch fails
loudly at test time instead of producing two different answers at runtime.

## Values

`OUR_ABA` mirrors the debtor routing number in Doina's `wire_domestic` sample
(`propose_payments.json`), so her worked example is reproducible end to end. It carries a
valid ABA mod-10 checksum, which matters because step 3 validates checksums.
"""

from __future__ import annotations

OUR_BIC = "LEAFUS33"
OUR_BANK_NAME = "Leafy Bank"
# ISO-3166 alpha-2. The originating-bank operand of Doina's L439 comparison.
OUR_BANK_COUNTRY = "US"
# ISO 20022 ClearingSystemMemberIdentification + the code identifying its scheme.
# `USABA` is a value of the spec's `clearingSystemCode` enum; do not invent others.
OUR_ABA = "021000021"
OUR_CLEARING_SYSTEM_CODE = "USABA"
