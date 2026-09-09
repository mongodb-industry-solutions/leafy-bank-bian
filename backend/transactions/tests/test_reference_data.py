"""Stage 3 step 1 — the reference-data port, its Mongo adapter, and the seed files.

Doc 17 §3 step 1's gate, as tests. Four groups:

1. **Layering** — `ports/` imports no pymongo; `domain/` imports no `adapters/`. This is
   doc 09's one rule, and the reason the port exists at all. Asserted by reading source,
   not by importing, so a violation is reported as a name rather than an ImportError.
2. **The port's own semantics** — `NullReferenceData` and `InMemoryReferenceData`.
   `InMemoryReferenceData` is the reference behaviour the adapter must match.
3. **The adapter** — against a stub collection that records the query it was given, so the
   projection and the `recordType` scope are both asserted. NOTE: this is not a substitute
   for the seeded-collection check in the manual gate; see the module note at the bottom.
4. **The seed files** — every purpose code is in the canonical spec's enum (defect
   2026-04-28: enums must be sourced from the spec, never hand-rolled), every ABA carries a
   valid checksum (step 3 will validate it), and the bank seed covers the frontend's
   autofill pool (or every autopopulated wire logs a directory miss).
"""

from __future__ import annotations

import ast
import json
import re
from pathlib import Path

import pytest

from contexts.payment_order_initiation.adapters.mongo_reference_data import (
    BIC_DIRECTORY,
    MongoReferenceData,
)
from contexts.payment_order_initiation.domain import bank_identity
from contexts.payment_order_initiation.ports.reference_data import (
    BankRecord,
    InMemoryReferenceData,
    NullReferenceData,
    PurposeCodeRecord,
)

TRANSACTIONS_ROOT = Path(__file__).resolve().parents[1]
CONTEXT = TRANSACTIONS_ROOT / "contexts" / "payment_order_initiation"
SEED_DIR = TRANSACTIONS_ROOT.parents[0] / "data" / "seed"


# --------------------------------------------------------------------------- #
# 1. Layering
# --------------------------------------------------------------------------- #

def _imported_modules(path: Path) -> set[str]:
    """Every module name this file actually imports.

    Parsed with `ast` rather than grepped: these files document the layering rule in their
    own docstrings, so a substring search reports the explanation as a violation. Only real
    `import` / `from ... import` statements count.
    """
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _violations(path: Path, banned: tuple[str, ...]) -> set[str]:
    return {
        module
        for module in _imported_modules(path)
        for prefix in banned
        if module == prefix or module.startswith(prefix + ".") or f".{prefix}." in module
    }


def test_ports_import_no_infrastructure():
    """A port that imports pymongo is not a port. Doc 09: dependencies point inward."""
    banned = ("pymongo", "bson", "fastapi", "adapters")
    for path in (CONTEXT / "ports").glob("*.py"):
        bad = _violations(path, banned)
        assert not bad, f"ports/{path.name} imports {sorted(bad)}"


def test_domain_never_imports_adapters():
    """`domain/` must not reach outward — the invariant that keeps stage 3 unit-testable.

    Doc 12 §3: *"If `enrich_payment.py` is pure and takes a `ReferenceData` port, that
    growth is additive and unit-testable with no database."*
    """
    for path in (CONTEXT / "domain").glob("*.py"):
        bad = _violations(path, ("adapters", "pymongo", "fastapi"))
        assert not bad, f"domain/{path.name} imports {sorted(bad)}"


# --------------------------------------------------------------------------- #
# 2. The port
# --------------------------------------------------------------------------- #

def test_null_reference_data_resolves_nothing_and_raises_nothing():
    """The safe default: a service with no store still runs the saga (doc 17 B7)."""
    store = NullReferenceData()
    assert store.bank_by_bic("CHASUS33") is None
    assert store.bank_by_clearing_member("USABA", "121000248") is None
    assert store.purpose_code("SUPP") is None


def _in_memory() -> InMemoryReferenceData:
    return InMemoryReferenceData(
        banks=[
            BankRecord(
                bic="CHASUS33",
                bank_name="JPMorgan Chase Bank, N.A.",
                bank_country="US",
                clearing_system_code="USABA",
                clearing_system_member_id="121000248",
            )
        ],
        purpose_codes=[
            PurposeCodeRecord(code="SUPP", name="Supplier Payment", description="…")
        ],
    )


def test_in_memory_resolves_by_bic_and_by_clearing_member():
    store = _in_memory()
    by_bic = store.bank_by_bic("CHASUS33")
    by_member = store.bank_by_clearing_member("USABA", "121000248")
    assert by_bic is not None and by_bic.bank_name == "JPMorgan Chase Bank, N.A."
    assert by_member == by_bic


def test_in_memory_lookups_are_case_insensitive():
    """Callers upper-case inconsistently; a miss caused by casing is the least useful one."""
    store = _in_memory()
    assert store.bank_by_bic("chasus33") is not None
    assert store.purpose_code("supp") is not None


@pytest.mark.parametrize("value", ["", None])
def test_in_memory_tolerates_empty_input(value):
    """An unenriched payment has `creditor.bic: None` — that is a miss, not a crash."""
    store = _in_memory()
    assert store.bank_by_bic(value) is None
    assert store.purpose_code(value) is None
    assert store.bank_by_clearing_member("USABA", value) is None
    assert store.bank_by_clearing_member(value, "121000248") is None


def test_in_memory_miss_returns_none():
    store = _in_memory()
    assert store.bank_by_bic("BARCGB22") is None
    assert store.purpose_code("SALA") is None


# --------------------------------------------------------------------------- #
# 3. The adapter
# --------------------------------------------------------------------------- #

class _StubCollection:
    """Records the (query, projection) it was asked for and returns a canned document."""

    def __init__(self, doc=None, raise_exc=None):
        self.doc = doc
        self.raise_exc = raise_exc
        self.calls = []

    def find_one(self, query, projection=None):
        self.calls.append((query, projection))
        if self.raise_exc:
            raise self.raise_exc
        return self.doc


class _StubDb:
    def __init__(self, banks=None, purpose_codes=None):
        self._c = {
            "correspondentBanks": banks or _StubCollection(),
            "purposeCodes": purpose_codes or _StubCollection(),
        }

    def __getitem__(self, name):
        return self._c[name]


_CHASE_DOC = {
    "swiftCode": "CHASUS33",
    "bankName": "JPMorgan Chase Bank, N.A.",
    "country": "US",
    "clearingSystemCode": "USABA",
    "clearingSystemMemberId": "121000248",
    "city": "New York",
}


def test_adapter_maps_a_bank_document_to_the_port_record():
    banks = _StubCollection(_CHASE_DOC)
    record = MongoReferenceData(_StubDb(banks=banks)).bank_by_bic("chasus33")

    assert record == BankRecord(
        bic="CHASUS33",
        bank_name="JPMorgan Chase Bank, N.A.",
        bank_country="US",
        clearing_system_code="USABA",
        clearing_system_member_id="121000248",
        city="New York",
    )


def test_adapter_scopes_every_bank_query_to_our_record_type():
    """JP/IN rows belong to another demo and must never come back as a US bank."""
    banks = _StubCollection(_CHASE_DOC)
    store = MongoReferenceData(_StubDb(banks=banks))
    store.bank_by_bic("CHASUS33")
    store.bank_by_clearing_member("USABA", "121000248")

    for query, _ in banks.calls:
        assert query["recordType"] == BIC_DIRECTORY


def test_adapter_upper_cases_the_bic_in_the_query():
    banks = _StubCollection(_CHASE_DOC)
    MongoReferenceData(_StubDb(banks=banks)).bank_by_bic("chasus33")
    assert banks.calls[0][0]["swiftCode"] == "CHASUS33"


def test_adapter_projections_are_inclusion_allowlists():
    """Defect 2026-08-31: on a shared database an exclusion list silently stops working.

    `purposeCodes` carries a 1024-dim `embedding`; it must never come back for a lookup.
    """
    banks = _StubCollection(_CHASE_DOC)
    codes = _StubCollection({"code": "SUPP", "name": "Supplier Payment", "description": "…"})
    store = MongoReferenceData(_StubDb(banks=banks, purpose_codes=codes))
    store.bank_by_bic("CHASUS33")
    store.purpose_code("SUPP")

    for stub in (banks, codes):
        projection = stub.calls[0][1]
        assert projection["_id"] == 0
        included = {k: v for k, v in projection.items() if k != "_id"}
        assert included and all(v == 1 for v in included.values()), projection
        assert "embedding" not in projection


def test_adapter_treats_a_row_with_no_bic_as_a_miss():
    """`BankRecord.bic` is required; fabricating one would be worse than resolving nothing."""
    banks = _StubCollection({"bankName": "Nameless Bank", "country": "US"})
    assert MongoReferenceData(_StubDb(banks=banks)).bank_by_bic("CHASUS33") is None


def test_adapter_resolves_nothing_when_the_collections_do_not_exist():
    """A database with no reference data seeded is a legitimate state, not a failure.

    Construction must touch nothing, so a service that never enriches never reaches for a
    reference collection. This is what makes the adapter safe to build unconditionally in
    `PaymentsService.__init__`.
    """

    class _EmptyDb:
        def __getitem__(self, name):
            raise KeyError(name)

    store = MongoReferenceData(_EmptyDb())
    assert store.bank_by_bic("CHASUS33") is None
    assert store.purpose_code("SUPP") is None
    assert store.bank_by_clearing_member("USABA", "121000248") is None


def test_adapter_survives_a_driver_error():
    """A reference-data outage degrades to WARN checks; it never fails a payment."""
    from pymongo.errors import PyMongoError

    banks = _StubCollection(raise_exc=PyMongoError("no primary"))
    codes = _StubCollection(raise_exc=PyMongoError("no primary"))
    store = MongoReferenceData(_StubDb(banks=banks, purpose_codes=codes))
    assert store.bank_by_bic("CHASUS33") is None
    assert store.purpose_code("SUPP") is None


def test_adapter_tolerates_a_purpose_row_missing_its_optional_fields():
    codes = _StubCollection({"code": "SUPP"})
    record = MongoReferenceData(_StubDb(purpose_codes=codes)).purpose_code("SUPP")
    assert record == PurposeCodeRecord(code="SUPP", name="", description="", category=None)


# --------------------------------------------------------------------------- #
# 4. The seed files
# --------------------------------------------------------------------------- #

def _seed(collection: str) -> list[dict]:
    payload = json.loads((SEED_DIR / f"leafy_bank_bian.{collection}.json").read_text())
    return payload["records"]


def test_seed_files_carry_notes_and_records_not_a_bare_array():
    """`_notes` must never be loadable as a document — hence the object shape."""
    for collection in ("purposeCodes", "correspondentBanks"):
        payload = json.loads(
            (SEED_DIR / f"leafy_bank_bian.{collection}.json").read_text()
        )
        assert set(payload) == {"_notes", "records"}
        assert isinstance(payload["records"], list) and payload["records"]


def test_every_seeded_purpose_code_is_in_the_canonical_spec_enum():
    """Defect 2026-04-28: enums come from the spec's `enum` arrays, never from memory."""
    spec_enum = _canonical_purpose_code_enum()
    seeded = [r["code"] for r in _seed("purposeCodes")]
    assert set(seeded) <= set(spec_enum), set(seeded) - set(spec_enum)
    # The seed is complete, and in spec order — so a spec change shows up as a diff here.
    assert seeded == spec_enum


def test_seeded_purpose_codes_are_unique_and_complete_rows():
    records = _seed("purposeCodes")
    codes = [r["code"] for r in records]
    assert len(codes) == len(set(codes))
    for r in records:
        # `code`, `name`, `description` are `required` in the canonical spec.
        assert r["name"] and r["description"], r
        # Deferred deliberately (doc 17 §6) — semantic matching is not in Phase 1.
        assert "embedding" not in r


def test_seeded_purpose_codes_include_the_flagship_code():
    """`SUPP` is the code in Doina's own `wire_domestic` sample and her demo script."""
    assert "SUPP" in {r["code"] for r in _seed("purposeCodes")}


def test_bank_seed_covers_our_own_bank_and_the_sample_beneficiary():
    """Without LEAFUS33 our outbound wire has no ABA — the exact gap D10 named."""
    by_bic = {r["swiftCode"]: r for r in _seed("correspondentBanks")}
    assert "LEAFUS33" in by_bic
    assert "CHASUS33" in by_bic
    # Her sample's two routing numbers, so the sample is reproducible end to end.
    assert by_bic["LEAFUS33"]["clearingSystemMemberId"] == "021000021"
    assert by_bic["CHASUS33"]["clearingSystemMemberId"] == "121000248"


def _autofill_pool() -> list[dict]:
    """`EXTERNAL_RECIPIENTS` from the wizard, parsed rather than duplicated here.

    Duplicating it would create a third copy of the same facts (seed, wizard, test) and the
    test would stop noticing when the wizard drifts — which is the whole thing it exists for.
    """
    wizard = (
        TRANSACTIONS_ROOT.parents[1]
        / "frontend" / "components" / "PaymentsWorkflow" / "InitiateWizard.js"
    ).read_text()
    block = re.search(r"EXTERNAL_RECIPIENTS\s*=\s*\[(.*?)\];", wizard, re.S)
    assert block, "EXTERNAL_RECIPIENTS not found — autofill pool moved; update this test"

    rows = []
    for line in block.group(1).splitlines():
        if "bic:" not in line:
            continue
        rows.append({
            key: match.group(1)
            for key, match in (
                (k, re.search(rf'{k}:\s*"([^"]*)"', line))
                for k in ("bic", "country", "clearingSystemCode", "clearingSystemMemberId")
            )
            if match
        })
    assert rows, "parsed no banks out of EXTERNAL_BANKS"
    return rows


def test_bank_seed_covers_the_frontend_autofill_pool():
    """Any autofill BIC without a row makes every autopopulated wire log a miss.

    Correct behaviour, but it reads as a broken demo — so the two lists must stay in step.
    """
    autofill = {r["bic"] for r in _autofill_pool()}
    seeded = {r["swiftCode"] for r in _seed("correspondentBanks")}
    assert autofill <= seeded, autofill - seeded


def test_autofill_pool_matches_the_bank_directory():
    """The pool must carry the directory's REAL values, not plausible-looking ones.

    Fixed 2026-08-31: autofill generated `digits(9)` for `clearingSystemMemberId`, so stage-3
    enrichment resolved the directory row and *corrected* it — and every autopopulated wire
    showed a spurious `from -> to` row in the progressive-enrichment panel. That panel is the
    one screen the stage exists for; a fabricated correction in it is worse than no
    enrichment at all, because the audience cannot tell which rows are real.

    Enrichment only ever fills fields that started empty when these two agree.
    """
    seeded = {r["swiftCode"]: r for r in _seed("correspondentBanks")}
    for bank in _autofill_pool():
        row = seeded.get(bank["bic"])
        assert row, f"{bank['bic']} is in the autofill pool but not the directory"
        for pool_key, seed_key in (
            ("country", "country"),
            ("clearingSystemCode", "clearingSystemCode"),
            ("clearingSystemMemberId", "clearingSystemMemberId"),
        ):
            assert bank.get(pool_key) == row.get(seed_key), (
                f"{bank['bic']}.{pool_key}: autofill {bank.get(pool_key)!r} != "
                f"directory {row.get(seed_key)!r}"
            )


def test_the_autofill_pool_no_longer_generates_a_routing_number():
    """A regression guard on the actual bug, not just on the values.

    Re-adding `digits(9)` to the member id would pass the parity test above (which reads the
    literal array) while restoring the fabricated-correction behaviour at runtime.
    """
    wizard = (
        TRANSACTIONS_ROOT.parents[1]
        / "frontend" / "components" / "PaymentsWorkflow" / "InitiateWizard.js"
    ).read_text()
    assert "clearingSystemMemberId: digits(" not in wizard


def test_every_seeded_bank_row_is_shaped_for_the_adapter():
    for r in _seed("correspondentBanks"):
        assert r["recordType"] == BIC_DIRECTORY
        # `recordType`, `country`, `identification` are `required` in the canonical spec.
        assert r["country"] and r["identification"]["value"]
        # The adapter keys on `swiftCode`; `identification.value` must agree with it.
        assert r["swiftCode"] == r["identification"]["value"]
        assert r["identification"]["scheme"] == "BIC"


def test_seeded_us_routing_numbers_pass_the_aba_checksum():
    """Step 3 validates ABA checksums, so a seed row that fails one is a live trap."""
    for r in _seed("correspondentBanks"):
        if r.get("clearingSystemCode") != "USABA":
            continue
        aba = r["clearingSystemMemberId"]
        assert len(aba) == 9 and aba.isdigit(), r
        d = [int(c) for c in aba]
        checksum = (
            3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])
        )
        assert checksum % 10 == 0, f"{r['swiftCode']} ABA {aba} fails mod-10"


def test_seeded_clearing_system_codes_are_in_the_payments_spec_enum():
    """These land on `debtor`/`creditor.clearingSystemCode`, whose enum is fixed."""
    allowed = {"USABA", "USPID", "GBDSC", "CHBCC", "DEBLZ", "CACPA"}
    for r in _seed("correspondentBanks"):
        code = r.get("clearingSystemCode")
        assert code in allowed, f"{r['swiftCode']}: {code} is not a spec enum value"


# --------------------------------------------------------------------------- #
# 5. Our own bank identity (step 2)
# --------------------------------------------------------------------------- #

def test_our_bank_identity_has_exactly_one_definition():
    """Doc 17 B6: the point of `bank_identity.py` is that no other module defines these.

    Step 2 found a fourth and fifth literal in `payment_rail/documents.py` that B6 had not
    accounted for. This test is what stops a sixth.
    """
    backend = TRANSACTIONS_ROOT
    offenders = []
    for path in backend.rglob("*.py"):
        if any(part in {".venv", "__pycache__", "tests"} for part in path.parts):
            continue
        if path.name == "bank_identity.py":
            continue
        if bank_identity.OUR_BIC in path.read_text():
            offenders.append(str(path.relative_to(backend)))
    assert not offenders, f"{bank_identity.OUR_BIC} is hardcoded in {offenders}"


def test_our_aba_passes_the_checksum_and_mirrors_doinas_sample():
    """Step 3 validates ABA checksums, and her `wire_domestic` debtor uses this number."""
    aba = bank_identity.OUR_ABA
    assert aba == "021000021"
    d = [int(c) for c in aba]
    assert (3 * (d[0] + d[3] + d[6]) + 7 * (d[1] + d[4] + d[7]) + (d[2] + d[5] + d[8])) % 10 == 0


def test_our_clearing_system_code_is_a_spec_enum_value():
    assert bank_identity.OUR_CLEARING_SYSTEM_CODE in {
        "USABA", "USPID", "GBDSC", "CHBCC", "DEBLZ", "CACPA"
    }


def test_our_identity_agrees_with_our_row_in_the_seeded_directory():
    """Two sources for the same fact, so they are asserted equal rather than derived.

    Doc 17 B6 kept the constant hardcoded (the money path must not depend on a lookup for
    our own BIC) and the directory also carries a LEAFUS33 row, because enrichment resolves
    banks uniformly. A mismatch would give two different answers at runtime; this fails it
    at test time instead.
    """
    ours = next(
        r for r in _seed("correspondentBanks") if r["swiftCode"] == bank_identity.OUR_BIC
    )
    assert ours["bankName"] == bank_identity.OUR_BANK_NAME
    assert ours["country"] == bank_identity.OUR_BANK_COUNTRY
    assert ours["clearingSystemMemberId"] == bank_identity.OUR_ABA
    assert ours["clearingSystemCode"] == bank_identity.OUR_CLEARING_SYSTEM_CODE


def test_bank_identity_stays_pure():
    """It is imported by `domain/` and by `payment_rail/`; it must import nothing outward."""
    path = CONTEXT / "domain" / "bank_identity.py"
    assert not _violations(path, ("pymongo", "bson", "fastapi", "adapters"))


def _canonical_purpose_code_enum() -> list[str]:
    """The `purposeCodes.code` enum, read from the canonical consolidated spec.

    Skips rather than fails when the umbrella workspace is not present, so this suite
    stays runnable from a standalone checkout.
    """
    root = TRANSACTIONS_ROOT.parents[4] / "bian-data-model"
    candidates = sorted(root.glob("Consolidated_*.json")) if root.is_dir() else []
    if not candidates:
        pytest.skip("canonical consolidated spec not available in this checkout")

    spec = json.loads(candidates[-1].read_text())
    collections = spec["collections"]
    if isinstance(collections, dict):
        purpose = collections["purposeCodes"]
    else:
        purpose = next(c for c in collections if c.get("name") == "purposeCodes")
    return purpose["validator"]["$jsonSchema"]["properties"]["code"]["enum"]


# --------------------------------------------------------------------------- #
# What these tests do NOT cover
# --------------------------------------------------------------------------- #
#
# The adapter is graded here against a stub, which proves the query shape and the mapping
# but NOT that a real seeded collection answers it. That gap is exactly defect 2026-08-31
# (`_ASSERTION`): a consumer graded against a fixture no producer could produce. Doc 17
# §3 step 1's gate therefore also requires:
#
#     python backend/data/load_reference_seed.py --db <db> --verify
#
# against the live cluster, which reads the same two collections through the same keys the
# adapter uses. Run it after every seed change.
