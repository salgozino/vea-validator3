"""Detector: event sync, reconstruction, reconciliation, verdict wiring."""

import pytest

from vea_challenger.claims import ZERO_ADDRESS, Claim, Party, hash_claim
from vea_challenger.contracts import L2View
from vea_challenger.detector import Detector
from vea_challenger.store import (
    CHALLENGED,
    CHALLENGED_BY_OTHER,
    DESYNC,
    HONEST,
    LOST,
    MISSED,
    RESOLVED,
    SEEN,
    UNDECIDED,
    WITHDRAWN,
    ClaimRow,
    Store,
)
from vea_challenger.verdict import Verdict

from conftest import FakeInbox, FakeNotifier, FakeOutbox

CLAIMER = "0x00000000000000000000000000000000000000A1"
US = "0x00000000000000000000000000000000000000C3"
OTHER = "0x00000000000000000000000000000000000000D4"
ROOT_A = b"\xaa" * 32
ROOT_B = b"\xbb" * 32

EPOCH = 100
PERIOD = 7_200
EPOCH_END = (EPOCH + 1) * PERIOD


@pytest.fixture
def rig(tmp_path, params):
    store = Store(tmp_path / "t.db")
    outbox = FakeOutbox()
    inbox = FakeInbox()
    notifier = FakeNotifier()
    detector = Detector(
        store, outbox, inbox, params, notifier, US, devnet=False,
        log_scan_chunk=5_000, genesis_lookback_blocks=1_000,
    )
    return store, outbox, inbox, notifier, detector


def _claimed_event(epoch=EPOCH, block=500, claimer=CLAIMER, root=ROOT_A):
    return {
        "name": "Claimed",
        "args": {"_claimer": claimer, "_epoch": epoch, "_stateRoot": root},
        "block_number": block,
        "log_index": 0,
        "tx_hash": "0xclaimtx",
    }


def test_claimed_event_creates_row(rig):
    store, outbox, _, notifier, detector = rig
    outbox.events = [_claimed_event()]
    outbox.block_timestamps[500] = EPOCH_END + 60
    detector.sync_events()

    row = store.get_claim(EPOCH)
    assert row.status == SEEN
    assert row.claim == Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60)
    assert store.get_cursor("outbox_events") == outbox.confirmed_head()


def test_duplicate_scan_is_idempotent(rig):
    store, outbox, _, _, detector = rig
    outbox.events = [_claimed_event()]
    outbox.block_timestamps[500] = EPOCH_END + 60
    detector.sync_events()
    outbox.head += 10
    detector.sync_events()  # cursor advanced past the event; nothing rescanned
    outbox.head += 10
    # even a forced rescan must not clobber state
    store.set_cursor("outbox_events", 0)
    detector.sync_events()
    assert len(store.all_claims()) == 1


def test_verification_started_updates_struct(rig):
    store, outbox, _, _, detector = rig
    outbox.events = [
        _claimed_event(),
        {
            "name": "VerificationStarted",
            "args": {"_epoch": EPOCH},
            "block_number": 600,
            "log_index": 0,
            "tx_hash": "0xv",
        },
    ]
    outbox.block_timestamps[500] = EPOCH_END + 60
    outbox.block_timestamps[600] = EPOCH_END + 90_000
    detector.sync_events()
    row = store.get_claim(EPOCH)
    assert row.claim.timestamp_verification == EPOCH_END + 90_000
    assert row.claim.blocknumber_verification == 600


def test_challenged_by_us_and_by_other(rig):
    store, outbox, _, notifier, detector = rig
    outbox.events = [
        _claimed_event(epoch=100, block=500),
        _claimed_event(epoch=101, block=501),
        {
            "name": "Challenged",
            "args": {"_epoch": 100, "_challenger": US},
            "block_number": 510,
            "log_index": 0,
            "tx_hash": "0xc1",
        },
        {
            "name": "Challenged",
            "args": {"_epoch": 101, "_challenger": OTHER},
            "block_number": 511,
            "log_index": 0,
            "tx_hash": "0xc2",
        },
    ]
    outbox.block_timestamps |= {500: EPOCH_END + 60, 501: EPOCH_END + 60}
    detector.sync_events()
    assert store.get_claim(100).status == CHALLENGED
    assert store.get_claim(100).claim.challenger == US
    assert store.get_claim(101).status == CHALLENGED_BY_OTHER


def test_verified_probes_winner_we_won(rig):
    store, outbox, _, notifier, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60, challenger=US)
    store.upsert_claim(ClaimRow(epoch=EPOCH, status=CHALLENGED, claim=base))
    won = Claim(
        state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60,
        challenger=US, honest=Party.CHALLENGER,
    )
    outbox.claim_hashes[EPOCH] = won.hash()
    outbox.events = [
        {"name": "Verified", "args": {"_epoch": EPOCH}, "block_number": 700, "log_index": 0, "tx_hash": "0xv"},
    ]
    detector.sync_events()
    row = store.get_claim(EPOCH)
    assert row.status == RESOLVED
    assert row.claim.honest == Party.CHALLENGER


def test_verified_probes_winner_we_lost(rig):
    store, outbox, _, notifier, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60, challenger=US)
    store.upsert_claim(ClaimRow(epoch=EPOCH, status=CHALLENGED, claim=base))
    lost = Claim(
        state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60,
        challenger=US, honest=Party.CLAIMER,
    )
    outbox.claim_hashes[EPOCH] = lost.hash()
    outbox.events = [
        {"name": "Verified", "args": {"_epoch": EPOCH}, "block_number": 700, "log_index": 0, "tx_hash": "0xv"},
    ]
    detector.sync_events()
    assert store.get_claim(EPOCH).status == LOST
    assert notifier.has("CRITICAL", "LOST")


def test_verified_unchallenged_marks_honest(rig):
    store, outbox, _, _, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60)
    store.upsert_claim(ClaimRow(epoch=EPOCH, status=UNDECIDED, claim=base))
    verified = Claim(
        state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60,
        honest=Party.CLAIMER,
    )
    outbox.claim_hashes[EPOCH] = verified.hash()
    outbox.events = [
        {"name": "Verified", "args": {"_epoch": EPOCH}, "block_number": 700, "log_index": 0, "tx_hash": "0xv"},
    ]
    detector.sync_events()
    assert store.get_claim(EPOCH).status == HONEST


def test_failed_resolution_resets_to_challenged(rig):
    store, outbox, _, notifier, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60, challenger=US)
    store.upsert_claim(
        ClaimRow(epoch=EPOCH, status="SNAPSHOT_SENT", claim=base, snapshot_tx="0xs", l2tol1_json="{}")
    )
    outbox.events = [
        {"name": "FailedResolution", "args": {"_epoch": EPOCH}, "block_number": 800, "log_index": 0, "tx_hash": "0xf"},
    ]
    detector.sync_events()
    row = store.get_claim(EPOCH)
    assert row.status == CHALLENGED
    assert row.snapshot_tx is None
    assert notifier.has("CRITICAL", "FAILED")


def test_reconcile_hash_gone_after_resolved(rig):
    store, outbox, _, _, detector = rig
    base = Claim(
        state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60,
        challenger=US, honest=Party.CHALLENGER,
    )
    row = ClaimRow(epoch=EPOCH, status=RESOLVED, claim=base)
    store.upsert_claim(row)
    # claimHashes deleted: someone called withdrawChallengeDeposit for us
    detector.reconcile(row)
    assert store.get_claim(EPOCH).status == WITHDRAWN


def test_reconcile_probes_escape_hatch_mutation(rig):
    store, outbox, _, _, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60, challenger=US)
    row = ClaimRow(epoch=EPOCH, status=CHALLENGED, claim=base)
    store.upsert_claim(row)
    mutated = Claim(
        state_root=ROOT_A, claimer=ZERO_ADDRESS, timestamp_claimed=EPOCH_END + 60, challenger=US
    )
    outbox.claim_hashes[EPOCH] = mutated.hash()
    out = detector.reconcile(row)
    assert out.claim.claimer == ZERO_ADDRESS
    assert store.get_claim(EPOCH).claim.claimer == ZERO_ADDRESS


def test_reconcile_desync_alerts(rig):
    store, outbox, _, notifier, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60)
    row = ClaimRow(epoch=EPOCH, status=SEEN, claim=base)
    store.upsert_claim(row)
    outbox.claim_hashes[EPOCH] = b"\xee" * 32
    detector.reconcile(row)
    assert store.get_claim(EPOCH).status == DESYNC
    assert notifier.has("CRITICAL", "reconstruct")


def test_evaluate_challenge_and_honest(rig, params):
    store, outbox, inbox, _, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60)
    row = ClaimRow(epoch=EPOCH, status=SEEN, claim=base)
    store.upsert_claim(row)

    inbox.finalized = (ROOT_B, L2View(timestamp=EPOCH_END + 10, number=1))
    assert detector.evaluate(row) == Verdict.CHALLENGE
    assert store.get_claim(EPOCH).true_root == "0x" + ROOT_B.hex()

    inbox.finalized = (ROOT_A, L2View(timestamp=EPOCH_END + 10, number=1))
    row = store.get_claim(EPOCH)
    assert detector.evaluate(row) == Verdict.HONEST
    assert store.get_claim(EPOCH).status == HONEST


def test_evaluate_waits_and_marks_undecided(rig):
    store, outbox, inbox, _, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60)
    row = ClaimRow(epoch=EPOCH, status=SEEN, claim=base)
    store.upsert_claim(row)
    inbox.finalized = (ROOT_B, L2View(timestamp=EPOCH_END - 100, number=1))
    inbox.latest = (ROOT_B, L2View(timestamp=EPOCH_END + 100, number=2))  # inside backdate window
    assert detector.evaluate(row) == Verdict.WAIT
    assert store.get_claim(EPOCH).status == UNDECIDED


def test_evaluate_quorum_split_waits_and_alerts(rig):
    store, outbox, inbox, notifier, detector = rig
    base = Claim(state_root=ROOT_A, claimer=CLAIMER, timestamp_claimed=EPOCH_END + 60)
    row = ClaimRow(epoch=EPOCH, status=SEEN, claim=base)
    store.upsert_claim(row)
    inbox.quorum_split = True
    assert detector.evaluate(row) == Verdict.WAIT
    assert notifier.has("CRITICAL", "quorum split")
