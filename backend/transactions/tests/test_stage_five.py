"""Stage 5 — execution: wire / ISO 20022. Doc 19 §4.

Four kinds of test, per the playbook's Phase H:

* **pure domain** — the pacs.008 mapper and the two document builders, no database
* **behaviour** — one per R-row that changes runtime behaviour, driven through the saga
* **the seams** — the mapper invents nothing, and `paymentMessages` is not a second
  canonical payment (her L887 prohibition)
* **immutability + isolation** — AST walks, because a docstring saying "append-only" or
  "we never touch that other demo's collection" guarantees nothing

Spec-conformance and enum-parity live in `test_payment_document_spec.py`, with the rest of
the conformance suite. Neither collection stage 5 writes has a spec entry (Q35, Q38), so
both are held by shape tests here instead.
"""

import ast
import json
import pathlib

import pytest

from contexts.payment_rail import execute
from contexts.payment_rail.adapters.simulated_wire_rail import SimulatedWireRail
from contexts.payment_rail.domain import execution_documents, pacs008
from contexts.payment_rail.ports.rail import NullRailGateway, RailAck
from tests.test_payments_service import (  # reuse the fixtures, don't fork them
    EXTERNAL_CREDITOR,
    _initiate,
    db,          # noqa: F401 - pytest fixture
    service,     # noqa: F401 - pytest fixture
    CREDITOR,    # noqa: F401 - account id constant (ACC-credit01)
)

_BACKEND = pathlib.Path(__file__).resolve().parents[2]
_TRANSACTIONS = _BACKEND / "transactions"


def _initiate_external(svc, **over):
    return _initiate(
        svc,
        creditor_account_ref=None,
        creditor_party=EXTERNAL_CREDITOR,
        payment_rail="WIRE",
        **over,
    )


# --- R5: her ISO VIEW element list --------------------------------------------

# Her L555-562, verbatim and in her order. `PmtId` is ours (the "..." at L563) and is
# asserted separately, so this list stays exactly what she asked for.
HER_ELEMENTS = [
    ("GrpHdr", None),
    ("CdtTrfTxInf", None),
    ("CdtTrfTxInf", "Dbtr"),
    ("CdtTrfTxInf", "DbtrAcct"),
    ("CdtTrfTxInf", "CdtrAgt"),
    ("CdtTrfTxInf", "Cdtr"),
    ("CdtTrfTxInf", "CdtrAcct"),
    ("CdtTrfTxInf", "RmtInf"),
]

# pain.001-only elements. None of these exists in pacs.008, and one of them (`PmtMtd`) was
# written into the first build of the mapper — the 2026-04-28 `bian-mapping` class of mistake,
# caught only by checking the sibling demo's live pacs.008 format spec. The value guard cannot
# see it, because `PmtMtd: "TRF"` is a legal-looking constant; only an element-name guard can.
PAIN001_ONLY = ["PmtMtd", "PmtInf", "PmtInfId", "ReqdExctnDt", "InitgPty", "CtrlSum"]


def _txn(message):
    """The single credit transfer, through the ISO envelope."""
    return pacs008.body(message)["CdtTrfTxInf"][0]


@pytest.fixture
def wire_payment(service, db):  # noqa: F811
    """A real external wire, driven through the whole saga. Not a hand-built dict.

    Every stage-5 assertion below runs against what the running system actually produced —
    the 2026-09-01 `unreachable-control` lesson: a unit test proves the mapper works, only
    the saga proves the output is reachable.
    """
    return _initiate_external(service)


def test_the_pacs008_carries_every_element_doina_lists(wire_payment, db):  # noqa: F811
    body = pacs008.body(db["paymentExecutions"].docs[0]["message"])
    for group, child in HER_ELEMENTS:
        assert group in body, f"her L555-562 names {group}"
        if child is not None:
            assert _txn(db["paymentExecutions"].docs[0]["message"]).get(child) is not None, (
                f"her L555-562 names {group}/{child}"
            )


def test_payment_type_information_is_mapped_to_iso_names(wire_payment, db):  # noqa: F811
    """`PmtTpInf` used to be passed through with our camelCase keys (`serviceLevel.code`),
    putting a non-ISO sub-tree inside an ISO document. Every other element is renamed; so is
    this one. `CtgyPurp` comes from the top-level `categoryPurpose`, which the canonical spec
    says explicitly is where it lives."""
    txn = _txn(db["paymentExecutions"].docs[0]["message"])
    pmt_tp_inf = txn.get("PmtTpInf")
    if pmt_tp_inf is None:
        return  # nothing was supplied to map — a legal outcome, not a failure
    assert not {"serviceLevel", "localInstrument"} & set(pmt_tp_inf), (
        "our camelCase keys must not survive into the ISO message"
    )
    assert set(pmt_tp_inf) <= {"SvcLvl", "LclInstrm", "CtgyPurp", "InstrPrty", "ClrChanl"}
    if "SvcLvl" in pmt_tp_inf:
        assert isinstance(pmt_tp_inf["SvcLvl"], list), "SvcLvl is 0..n in pacs.008"


def test_the_message_is_wrapped_in_the_iso_envelope(wire_payment, db):  # noqa: F811
    """A pacs.008 is `Document/FIToFICstmrCdtTrf/{GrpHdr, CdtTrfTxInf}`, and `CdtTrfTxInf` is
    1..n. The first build emitted `{GrpHdr, CdtTrfTxInf}` flat with a single object — a
    different document shape from every real pacs.008.

    Verified against the sibling demo's own live format specification, whose every field path
    reads `Document.FIToFICstmrCdtTrf.…`
    (`repos/payments-processing/.../fsi-payments-processing.format_specifications.json`). A
    message we generate and one that demo converts from an MT103 now have the same shape.
    """
    message = db["paymentExecutions"].docs[0]["message"]
    assert list(message) == ["Document"]
    assert list(message["Document"]) == ["FIToFICstmrCdtTrf"]
    body = message["Document"]["FIToFICstmrCdtTrf"]
    assert set(body) == {"GrpHdr", "CdtTrfTxInf"}
    assert isinstance(body["CdtTrfTxInf"], list) and len(body["CdtTrfTxInf"]) == 1
    assert body["GrpHdr"]["NbOfTxs"] == "1"


def test_no_pain001_only_element_appears_in_the_pacs008(wire_payment, db):  # noqa: F811
    """The element-name guard the value guard cannot be.

    `PmtMtd` is `pain.001 PmtInf/PmtMtd` — our own canonical spec's `wireDetails.paymentMethod`
    description says so — and it does not exist in pacs.008. The first build put it inside
    `CdtTrfTxInf` with the legal-looking value `"TRF"`, so
    `test_every_iso_value_comes_from_the_canonical_payment` passed: the value was fine, the
    element was not. Initiation elements do not belong in an interbank message.
    """
    message = db["paymentExecutions"].docs[0]["message"]
    rendered = str(message)
    for element in PAIN001_ONLY:
        assert f"'{element}'" not in rendered, (
            f"{element} is a pain.001 element and has no place in a pacs.008"
        )


def test_the_message_identifies_itself(wire_payment, db):  # noqa: F811
    """`PmtId` is not in her element list — her "..." is. It is included because a pacs.008
    without one is unidentifiable on the network, and `UETR` is the field the whole ISO
    tracking story hangs on."""
    pmt_id = _txn(db["paymentExecutions"].docs[0]["message"])["PmtId"]
    assert pmt_id["UETR"] == wire_payment["uetr"]
    assert pmt_id["EndToEndId"] == wire_payment["endToEndId"]
    assert pmt_id["InstrId"] == wire_payment["instructionId"]
    assert pmt_id["TxId"] == wire_payment["txnId"]


# --- R1, R6: the mapper is a projection, and invents nothing -------------------

def _leaves(node, path=""):
    if isinstance(node, dict):
        for k, v in node.items():
            yield from _leaves(v, f"{path}/{k}")
    elif isinstance(node, list):
        for i, v in enumerate(node):
            yield from _leaves(v, f"{path}[{i}]")
    elif node is not None:
        yield path, node


def _payment_values(payment):
    """Every scalar anywhere in the payment document, flattened."""
    return {value for _, value in _leaves(payment)}


def test_every_iso_value_comes_from_the_canonical_payment(wire_payment, db):  # noqa: F811
    """R1/R6 — the mapper is a projection, not a second source of truth.

    Every leaf in the message is either a value that exists somewhere on the payment, or one
    of the named ISO constants. This is the direct guard against the 2026-04-28
    `bian-mapping` mistake (a domain-intuitive value invented into a BIAN field), and it is
    what makes her L565 claim — *"demonstrates the relationship between business-domain data
    and payment messaging"* — literally true rather than decorative.
    """
    message = db["paymentExecutions"].docs[0]["message"]
    allowed = _payment_values(wire_payment) | {
        pacs008._NB_OF_TXS,
        pacs008.SETTLEMENT_CLEARING,
        pacs008.SETTLEMENT_COVER,
        # Our own agent identity — a constant of the bank, not of the payment.
        "LEAFUS33", "Leafy Bank", "US", "021000021", "USABA",
    }
    for path, value in _leaves(message):
        assert value in allowed, f"{path} = {value!r} is on neither the payment nor the constant list"


def test_the_pacs008_reads_routing_settlement_from_the_snapshot_record(wire_payment, db):  # noqa: F811
    """FR-5.2 — the pacs.008's routing/settlement fields come from the routingSnapshots
    record (the immutable copy of stage 4's decision), not re-derived from the in-memory
    strategy at execution time. The value date and the instructing/instructed agents in the
    message match the persisted snapshot by value — proving the mapper reads the record rather
    than re-deriving."""
    snapshot = db["routingSnapshots"].docs[0]
    message = db["paymentExecutions"].docs[0]["message"]
    grp_hdr = pacs008.body(message)["GrpHdr"]

    # The interbank settlement date comes from the snapshot's valueDate.
    assert grp_hdr["IntrBkSttlmDt"] == snapshot["valueDate"]
    # The instructing agent (our bank) and instructed agent (creditor's bank) match the
    # snapshot's routing agents by value — read off the persisted record, not the strategy.
    assert grp_hdr["InstgAgt"]["FinInstnId"]["BICFI"] == snapshot["instructingAgent"]["bic"]
    assert grp_hdr["InstdAgt"]["FinInstnId"]["BICFI"] == snapshot["beneficiaryAgent"]["bic"]


def test_the_mapper_does_no_io():
    """R1 — *"only at the rail boundary"* is a claim about call sites, so the mapper must be
    callable with a dict and nothing else. An import of pymongo or `process` here would mean
    the transformation had leaked back into the domain."""
    source = (_TRANSACTIONS / "contexts/payment_rail/domain/pacs008.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.add(node.module or "")
    forbidden = [m for m in imported if m.split(".")[0] in {"pymongo", "bson", "process"}]
    assert not forbidden, f"the mapper must stay pure; it imports {forbidden}"


# --- B4: a book transfer maps nothing and writes neither artifact --------------

def test_a_book_transfer_writes_no_execution_artifacts(service, db):  # noqa: F811
    """Two independent pieces of evidence say so: her *"only at the rail boundary"*, and the
    spec's own `internal_transfer` sample sitting at SETTLED with
    `refs.paymentExecutionIds: []`. Doc 19 B4, Q39."""
    payment = _initiate(service, payment_rail="INTERNAL")

    assert payment["status"] == "SETTLED"
    assert db["paymentExecutions"].docs == []
    assert db["paymentMessages"].docs == []
    assert payment["refs"]["paymentExecutionIds"] == []
    assert payment["refs"]["canonicalJsonId"] is None


def test_an_internal_transfer_stamps_the_samples_own_clearing_values(service, db):  # noqa: F811
    """R11 — copied from the `internal_transfer` sampleDocument so her worked example stays
    reproducible end to end."""
    payment = _initiate(service, payment_rail="INTERNAL")
    clearing = payment["clearing"]
    assert clearing["networkRef"] == "INTERNAL-BOOK-TRANSFER"
    assert clearing["networkCode"] == "0000"
    assert clearing["settledAt"] is not None


def test_the_skipped_checks_say_why(service, db):  # noqa: F811
    """A SKIP with no reason is indistinguishable from a check nobody wrote. These four are
    the demo's own explanation of why a book transfer is cheaper — D9's point, made by the
    document instead of by a slide."""
    payment = _initiate(service, payment_rail="INTERNAL")
    stage_five = [c for c in payment["checks"] if c["stage"] == "5 execute"]
    skipped = {c["name"]: c for c in stage_five if c["result"] == "SKIP"}
    assert set(skipped) == {
        "iso20022_message_generated",
        "canonical_payload_stored",
        "rail_submission_acknowledged",
        "execution_recorded",
    }
    assert all(c["detail"] for c in skipped.values())


# --- B1, R12: the external wire runs stage 5 in full, then halts ---------------

def test_an_external_wire_reaches_in_progress_with_both_artifacts(wire_payment, db):  # noqa: F811
    assert wire_payment["status"] == "IN_PROGRESS"
    assert len(db["paymentExecutions"].docs) == 1
    assert len(db["paymentMessages"].docs) == 1

    execution = db["paymentExecutions"].docs[0]
    message = db["paymentMessages"].docs[0]
    assert wire_payment["refs"]["paymentExecutionIds"] == [execution["paymentExecutionId"]]
    assert wire_payment["refs"]["canonicalJsonId"] == message["paymentMessageId"]


def test_an_external_wire_writes_a_transaction_document_to_the_clearing_account(wire_payment, db):  # noqa: F811
    """Stage 7 doc 21 B1/B3 — the halt is gone. The external wire now writes a `transactions`
    doc with the clearing account as payee, so the ledger's `ingest_worker` sees a real
    `payee.accountId` (not null) and the payment reaches `ledgerEvents` via CDC.

    The payment stays at IN_PROGRESS — settlement is deferred to `settle.py` (B3) — but the
    money has moved: the debtor is debited and the clearing account is credited.
    """
    assert len(db["transactions"].docs) == 1
    txn = db["transactions"].docs[0]
    assert txn["payee"]["accountId"] == "ACC-CLEARING-WIRE"
    assert txn["payee"]["isInternal"] is False
    assert len(db["notifications"].docs) == 1


def test_execution_refuses_a_payment_that_is_not_approved(service, db):  # noqa: F811
    """R9 — her L538. The saga already enforces this by ordering; the guard makes it a
    property of the stage instead of a property of the sequence."""
    from process.payment_context import PaymentCollections, PaymentContext

    ctx = PaymentContext(
        customer_ref="CUST-x", debtor_account_ref="ACC-x", instructed_amount=1.0,
        instructed_currency="USD", payment_type="CREDIT_TRANSFER", payment_rail="WIRE",
        collections=PaymentCollections(
            db=db, customers=db["customers"], accounts=db["accounts"],
            payments=db["payments"], transactions=db["transactions"],
            notifications=db["notifications"],
        ),
        payment_oid="deadbeef", current_state="ROUTED",
    )
    with pytest.raises(ValueError, match="requires an approved payment"):
        execute.run(ctx)


# --- R8, B3: append-only per attempt ------------------------------------------

def test_each_payment_gets_its_own_attempt_sequence(service, db):  # noqa: F811
    """Two payments are two attempt-1s, not attempt 1 and 2 — the counter is per payment."""
    first = _initiate_external(service)
    second = _initiate_external(service)

    assert first["paymentId"] != second["paymentId"]
    assert sorted(d["attempt"] for d in db["paymentExecutions"].docs) == [1, 1]
    assert len({d["paymentExecutionId"] for d in db["paymentExecutions"].docs}) == 2


def test_a_further_attempt_on_the_same_payment_appends(service, db):  # noqa: F811
    """Her L862: *"Repairs, recalls, returns, and retries should create additional execution
    artifacts rather than overwrite the original."*

    ⚠️ **A second attempt has no trigger through the saga yet** — repairs, recalls and
    returns are stage 9, and an idempotent replay returns the existing payment before
    reaching the rail. So this drives `_next_attempt` against a seeded collection rather than
    pretending the saga can produce the outcome: the counter and the insert-only shape are
    built and correct, and the *path* that uses them arrives with stage 9. Asserting
    otherwise would be the 2026-09-01 `unreachable-control` mistake with the sign flipped —
    claiming an outcome the running system cannot reach.
    """
    payment = _initiate_external(service)
    payment_id = payment["paymentId"]
    assert db["paymentExecutions"].docs[0]["attempt"] == 1

    from process.payment_context import PaymentCollections, PaymentContext

    ctx = PaymentContext(
        customer_ref="CUST-x", debtor_account_ref="ACC-x", instructed_amount=1.0,
        instructed_currency="USD", payment_type="CREDIT_TRANSFER", payment_rail="WIRE",
        collections=PaymentCollections(
            db=db, customers=db["customers"], accounts=db["accounts"],
            payments=db["payments"], transactions=db["transactions"],
            notifications=db["notifications"],
            payment_executions=db["paymentExecutions"],
        ),
    )
    ctx.payment_id = payment_id
    assert execute._next_attempt(ctx) == 2, "the next attempt appends; it does not overwrite"


def test_the_rail_ack_records_both_statuses(wire_payment, db):  # noqa: F811
    """R10 — her L255-258: *"keep both: normalized internal status ... [and] original rail
    status, reason code, and message reference."*"""
    execution = db["paymentExecutions"].docs[0]
    assert execution["status"] == execution_documents.ACKNOWLEDGED
    rail_status = execution["railStatus"]
    assert rail_status["code"] == "ACSP"
    assert rail_status["reason"]
    assert rail_status["messageRef"]
    assert execution["acknowledgedAt"] is not None


def test_nothing_replaces_or_deletes_a_payment_execution():
    """B3 — an AST walk, because a docstring saying "append-only" guarantees nothing.

    The one permitted update is `_acknowledge`, over the three fields
    `execution_documents.acknowledge` builds. Anything else — a `replace_one`, a delete, or a
    second `update_one` site — is the bug this test exists to catch. Same device as stage 4's
    `test_nothing_ever_updates_a_routing_snapshot`.
    """
    source = (_TRANSACTIONS / "contexts/payment_rail/execute.py").read_text()
    tree = ast.parse(source)
    forbidden = {"replace_one", "delete_one", "delete_many", "find_one_and_replace",
                 "find_one_and_delete", "update_many"}
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (called & forbidden), f"{called & forbidden} must never touch an execution artifact"

    # `update_one` appears exactly twice: the `refs.paymentExecutionIds` push on `payments`,
    # and `_acknowledge`. A third would need justifying here.
    assert source.count(".update_one(") == 2


def test_the_acknowledgement_updates_only_three_named_fields():
    ack = RailAck(accepted=True, status_code="ACSP", reason="ok", message_ref="m")
    fields = execution_documents.acknowledge(ack, now=None)
    assert set(fields) == {"status", "railStatus", "acknowledgedAt"}


# --- B2: `paymentMessages` is hers, and is not a second canonical payment ------

def test_the_payment_message_is_not_a_second_canonical_payment(wire_payment, db):  # noqa: F811
    """Her L887: *"Do not allow both `payments` and [this collection] to be authoritative
    canonical payment collections."* So it carries no status, no balance, no lifecycle."""
    message = db["paymentMessages"].docs[0]
    for forbidden in ("status", "lifecycle", "balance", "checks", "fraud", "entitlement"):
        assert forbidden not in message, f"{forbidden} would make this a second payment record"
    assert message["paymentId"] == wire_payment["paymentId"]


def test_the_message_records_the_mapping_version_and_the_audit(wire_payment, db):  # noqa: F811
    """Her L892 names four uses; `mappingVersion` and `transformationAudit` are two of them."""
    message = db["paymentMessages"].docs[0]
    assert message["mappingVersion"] == pacs008.MAPPING_VERSION
    assert message["direction"] == execution_documents.OUTBOUND
    audit = message["transformationAudit"]
    assert any(e["source"] == "DERIVED" and "SttlmMtd" in e["element"] for e in audit), (
        "the one derived value must say it was derived (Q40)"
    )
    assert any(e["source"] == "PROJECTION" for e in audit)


def test_the_message_and_execution_reference_each_other_at_insert(wire_payment, db):  # noqa: F811
    """B2 — both oids are minted before either document is built, so neither collection needs
    a forward-reference update."""
    execution = db["paymentExecutions"].docs[0]
    message = db["paymentMessages"].docs[0]
    assert execution["paymentMessageId"] == message["paymentMessageId"]
    assert message["paymentExecutionId"] == execution["paymentExecutionId"]


def test_nothing_in_the_backend_touches_canonical_json_storage():
    """B2 — `canonicalJsonStorage` is fsi-payments-processing's, live with 33-34 documents on
    `ist-shared.leafy_bank_bian`. Doina's own target-model table renames the row to
    `paymentMessages` (L780), so we build ours and leave theirs alone. This test is what keeps
    a future convenience from quietly reaching into another demo's data.

    ⚠️ It checks **code**, not prose: several modules mention the collection in a comment
    saying exactly why they do not use it, and a grep-based version of this test failed on
    its own warnings. So the AST is walked and only string literals and attribute names
    count — which is also the only form that could actually reach the collection.
    """
    hits = []
    for path in _TRANSACTIONS.rglob("*.py"):
        if ".venv" in path.parts or path.name == "test_stage_five.py":
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            literal = (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and "canonicalJsonStorage" in node.value
                # A module or function docstring is prose, not a reference.
                and node.value.strip().count(" ") < 3
            )
            attribute = isinstance(node, ast.Attribute) and "canonical_json_storage" in node.attr
            if literal or attribute:
                hits.append(str(path.relative_to(_TRANSACTIONS)))
    assert not hits, f"canonicalJsonStorage belongs to another demo; referenced in {hits}"


# --- R13: the simulated rail ---------------------------------------------------

def test_the_simulated_rail_is_deterministic():
    """*"The demo will not connect to a real payment network"* — so the gateway must produce
    the same acknowledgement twice. A random network reference would make the reconciliation
    story unreproducible."""
    rail = SimulatedWireRail()
    message = {
        pacs008.ROOT: {
            pacs008.MESSAGE_ROOT: {
                "GrpHdr": {},
                "CdtTrfTxInf": [
                    {
                        "IntrBkSttlmAmt": {pacs008.TEXT_KEY: 1.0},
                        "CdtrAgt": {"FinInstnId": {"BICFI": "CHASUS33"}},
                    }
                ],
            }
        }
    }
    first = rail.submit(message, network="FEDWIRE", payment_id="PAY-abc12345")
    second = rail.submit(message, network="FEDWIRE", payment_id="PAY-abc12345")
    assert first == second
    assert first.accepted and first.simulated
    assert first.network_ref == "IMAD-abc12345"


def test_the_simulated_rail_rejects_a_message_missing_its_amount():
    rail = SimulatedWireRail()
    ack = rail.submit(
        {pacs008.ROOT: {pacs008.MESSAGE_ROOT: {"GrpHdr": {}, "CdtTrfTxInf": [{}]}}},
        network="SWIFT", payment_id="PAY-1",
    )
    assert not ack.accepted and ack.status_code == "RJCT"


def test_a_missing_gateway_refuses_rather_than_pretending(service, db):  # noqa: F811
    """The Null gateway must not look like an acknowledgement. An infrastructure gap is not
    the caller's error, so it FAILs the attempt through the ordinary path (doc 17 B7)."""
    service.rail_gateway = NullRailGateway()
    with pytest.raises(ValueError, match="Rail rejected"):
        _initiate_external(service)

    execution = db["paymentExecutions"].docs[0]
    assert execution["status"] == execution_documents.FAILED
    assert execution["railStatus"]["code"] == "RJCT"
    assert db["transactions"].docs == [], "a refused message moves no money"


# --- B6: the boundary field ----------------------------------------------------

def test_the_transaction_doc_gained_only_payment_execution_id(service, db):  # noqa: F811
    """§7 — stage 5's single boundary change, additive only. A key-set diff, because the
    firewall's rule is about the shape of this document and nothing else."""
    _initiate(service, payment_rail="INTERNAL")
    txn = db["transactions"].docs[0]

    # doc 12 §1's contract: exactly what the ledger's `ingest_worker` reads.
    for field in ("amount", "paymentId", "currency", "paymentType", "rail", "sourceSystem"):
        assert field in txn
    assert txn["payer"]["accountId"] and txn["payee"]["accountId"]
    assert "paymentExecutionId" in txn
    # Null on a book transfer: no execution artifact exists to point at (B4).
    assert txn["paymentExecutionId"] is None


# --- the durable fix: pin the message STRUCTURE to a source ---------------------

_FORMAT_SPEC = pathlib.Path(__file__).parent / "fixtures" / "pacs008_format_specification.json"


def _resolve(message, path):
    """Walk a `Document.FIToFICstmrCdtTrf.CdtTrfTxInf.PmtId.InstrId` path.

    `CdtTrfTxInf` is a list in our message and a bare path segment in the spec (it writes
    paths as though there were one transaction), so a list is entered at its first element —
    which is the only one we ever emit.
    """
    node = message
    for segment in path.split("."):
        if isinstance(node, list):
            if not node:
                return None
            node = node[0]
        if not isinstance(node, dict):
            return None
        node = node.get(segment)
    if isinstance(node, list):
        node = node[0] if node else None
    return node


def test_the_pacs008_matches_the_iso20022_format_spec(wire_payment, db):  # noqa: F811
    """Every `required` path in the pacs.008 format specification resolves in our message.

    **This test is the durable fix for defect 2026-09-02 `message-structure-unsourced`.** The
    first build got the envelope, the `CdtTrfTxInf` cardinality and the `PmtMtd` element wrong
    because the *structure* was written from memory while the *values* were rigorously
    sourced. This pins the structure to a source too.

    ⚠️ **What this does NOT prove.** The fixture is the fsi-payments-processing demo's
    **16-field subset**, not the ISO 20022 schema. No pacs.008 XSD exists anywhere in this
    workspace — they are behind ISO registration — so nothing here is schema validation. A
    green result means: our element paths agree with the one machine-readable pacs.008
    specification available locally, and a message we generate is comparable with one that
    demo converts from an MT103. It does not mean the message would pass an ISO validator.
    """
    spec = json.loads(_FORMAT_SPEC.read_text())
    message = db["paymentExecutions"].docs[0]["message"]

    missing = []
    for name, field in spec["supported_fields"].items():
        if not field.get("required"):
            continue
        if _resolve(message, field["path"]) in (None, ""):
            missing.append(f"{name} ({field['path']})")
    assert not missing, f"required pacs.008 paths absent from our message: {missing}"


def test_the_optional_spec_paths_we_populate_are_at_the_specs_paths(wire_payment, db):  # noqa: F811
    """The optional half — named explicitly, not inferred.

    An earlier version searched for each path's bare leaf name anywhere in the message and
    then demanded it at the spec's path. That conflated `DbtrAcct.Id.IBAN` with
    `CdtrAcct.Id.IBAN` and failed on a correct message. Naming the paths is duller and
    actually says something.

    `CdtrAcct.Id.IBAN` is deliberately absent: the external beneficiary is identified by
    account number, so it goes to `CdtrAcct.Id.Othr.Id` — legal ISO, and the shape a US
    beneficiary takes. Absence of an optional element is not a defect; emitting the same fact
    at a *different* path than the spec's would be, which is exactly what `IntrBkSttlmDt` on
    `CdtTrfTxInf` instead of `GrpHdr` was.
    """
    spec = json.loads(_FORMAT_SPEC.read_text())
    paths = {name: f["path"] for name, f in spec["supported_fields"].items()}
    message = db["paymentExecutions"].docs[0]["message"]

    for name in ("endToEndId", "chargeBearer", "debtorAccount"):
        assert _resolve(message, paths[name]) is not None, (
            f"{name} is populated, so it must be at the spec's path {paths[name]}"
        )

    # The one we knowingly place elsewhere, with the reason above.
    assert _resolve(message, paths["creditorAccount"]) is None
    assert _txn(message)["CdtrAcct"]["Id"]["Othr"]["Id"], "identified by account number instead"


# --- the XML rendering (stdlib, no dependency) ---------------------------------

def test_the_message_serialises_to_real_pacs008_xml(wire_payment, db):  # noqa: F811
    """Well-formed XML, correct namespace, attributes as attributes.

    ⚠️ Well-formed and pacs.008-shaped is not XSD-validated — see the format-spec test above.
    """
    from xml.etree import ElementTree as ET

    message = db["paymentExecutions"].docs[0]["message"]
    xml = pacs008.to_xml(message)

    root = ET.fromstring(xml)
    assert root.tag == f"{{{pacs008.NAMESPACE}}}Document"
    ns = {"p": pacs008.NAMESPACE}
    amount = root.find(".//p:IntrBkSttlmAmt", ns)
    # `Ccy` is an ATTRIBUTE, not a child element — the whole reason for the `@` convention.
    assert amount.get("Ccy") == wire_payment["currency"]
    assert amount.text == f"{wire_payment['amount']:.2f}", "two decimals, not Python's 250000.0"
    assert root.find(".//p:GrpHdr/p:IntrBkSttlmDt", ns) is not None
    assert len(root.findall(".//p:CdtTrfTxInf", ns)) == 1


def test_the_xml_omits_absent_optional_elements(wire_payment, db):  # noqa: F811
    """An absent optional element is correct ISO; an empty one is not. A `None` in the dict
    must produce no element at all, rather than `<Elem/>`."""
    message = pacs008.build({"paymentId": "PAY-1", "currency": "USD", "amount": 1.0})
    xml = pacs008.to_xml(message)
    assert "<Purp" not in xml, "no purpose code was supplied, so the element must not appear"
    assert "<RmtInf" not in xml
    assert "<IntrmyAgt1" not in xml, "no intermediary hop, so no element"


def test_the_xml_walk_knows_nothing_about_pacs008():
    """`to_xml` is a mechanical walk of the `@`/`#text` convention — the point of adopting a
    standard convention rather than an invented one. If it grew per-element knowledge, the
    convention would have stopped paying for itself."""
    tree = ast.parse((_TRANSACTIONS / "contexts/payment_rail/domain/pacs008.py").read_text())
    walkers = {"to_xml", "_append", "_text"}
    literals = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in walkers:
            # Docstrings are prose — a comment naming `CdtTrfTxInf` to explain the list case
            # is not knowledge of pacs.008 in the code. Only string LITERALS count, which is
            # the same correction the canonicalJsonStorage isolation test needed.
            body = node.body[1:] if ast.get_docstring(node) else node.body
            for child in body:
                literals += [
                    n.value for n in ast.walk(child)
                    if isinstance(n, ast.Constant) and isinstance(n.value, str)
                ]
    for element in ("GrpHdr", "CdtTrfTxInf", "IntrBkSttlmAmt", "Dbtr", "Cdtr", "PmtId"):
        assert element not in literals, f"the serialiser must not know about {element}"


# --- Stage 7 step 2 gate: external creditor routed to clearing account --------

def test_an_external_wire_resolves_the_clearing_account(wire_payment, db):
    """Stage 7 doc 21 B1, step 2 gate.

    An external wire has no Leafy Bank creditor account, but `_money_move` credits by
    `accountId` and `posting_rules` reads `account.gl.accountCode`. The routing resolves
    the rail's clearing account (`ACC-CLEARING-WIRE`) before `_money_move` fires, so the
    credit leg posts to a real account with a real GL mapping (1131).
    """
    assert wire_payment["status"] == "IN_PROGRESS"

    # The clearing account was credited — the routing found it and _money_move ran.
    clearing = db["accounts"].find_one({"accountId": "ACC-CLEARING-WIRE"})
    assert clearing is not None, "routing must resolve the clearing account"
    assert clearing["balance"]["available"] == 250.0, "clearing account holds the in-flight credit"


def test_an_internal_transfer_does_not_touch_the_clearing_account(service, db):
    """Stage 7 doc 21 B1, step 2 gate — the routing must not affect internal transfers.

    An internal transfer credits the named creditor account (ACC-credit01), not the
    clearing account. The routing only fires for `is_external_creditor`, which is False
    here. Proves the negative: the clearing account is not in the internal path.
    """
    payment = _initiate(service)  # INTERNAL rail, creditor = CREDITOR

    assert payment["status"] == "SETTLED"
    creditor = db["accounts"].find_one({"accountId": CREDITOR})
    assert creditor["balance"]["available"] == 10_250.0, "the named creditor was credited"
    clearing = db["accounts"].find_one({"accountId": "ACC-CLEARING-WIRE"})
    assert clearing["balance"]["available"] == 0, "clearing account untouched by internal transfers"
