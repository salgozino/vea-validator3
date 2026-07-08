"""Verdict logic: the safety-critical decision rules."""

from vea_challenger.verdict import (
    Verdict,
    VerdictInputs,
    decide,
    earliest_verification_ts,
)

EPOCH = 100
PERIOD = 7_200
SEQ_DELAY = 86_400
EPOCH_END = (EPOCH + 1) * PERIOD

ROOT_A = b"\xaa" * 32
ROOT_B = b"\xbb" * 32
ZERO = b"\x00" * 32


def _inputs(**kw) -> VerdictInputs:
    base = dict(
        epoch=EPOCH,
        claimed_root=ROOT_A,
        epoch_period=PERIOD,
        sequencer_delay=SEQ_DELAY,
        finalized_ts=None,
        finalized_snapshot=None,
        latest_ts=None,
        latest_snapshot=None,
    )
    base.update(kw)
    return VerdictInputs(**base)


def test_honest_when_finalized_matches():
    v = _inputs(finalized_ts=EPOCH_END, finalized_snapshot=ROOT_A)
    assert decide(v) == Verdict.HONEST


def test_challenge_when_finalized_differs():
    v = _inputs(finalized_ts=EPOCH_END + 1, finalized_snapshot=ROOT_B)
    assert decide(v) == Verdict.CHALLENGE


def test_challenge_when_no_snapshot_saved():
    """snapshots(E) == 0x0 makes any claim fraudulent."""
    v = _inputs(finalized_ts=EPOCH_END + 1, finalized_snapshot=ZERO)
    assert decide(v) == Verdict.CHALLENGE


def test_wait_when_finalized_before_epoch_end():
    """Snapshot can still be overwritten inside the epoch: no verdict."""
    v = _inputs(finalized_ts=EPOCH_END - 1, finalized_snapshot=ROOT_B)
    assert decide(v) == Verdict.WAIT


def test_wait_when_no_data():
    assert decide(_inputs()) == Verdict.WAIT


def test_latest_fallback_requires_sequencer_delay_buffer():
    """Backdating window: latest data younger than epochEnd + seqDelay decides nothing."""
    v = _inputs(latest_ts=EPOCH_END + SEQ_DELAY - 1, latest_snapshot=ROOT_B)
    assert decide(v) == Verdict.WAIT


def test_latest_fallback_after_backdating_window():
    v = _inputs(latest_ts=EPOCH_END + SEQ_DELAY, latest_snapshot=ROOT_B)
    assert decide(v) == Verdict.CHALLENGE
    v = _inputs(latest_ts=EPOCH_END + SEQ_DELAY, latest_snapshot=ROOT_A)
    assert decide(v) == Verdict.HONEST


def test_finalized_takes_priority_over_latest():
    """A finalized read past epoch end wins even if latest disagrees."""
    v = _inputs(
        finalized_ts=EPOCH_END,
        finalized_snapshot=ROOT_A,
        latest_ts=EPOCH_END + SEQ_DELAY,
        latest_snapshot=ROOT_B,
    )
    assert decide(v) == Verdict.HONEST


def test_stale_finalized_falls_through_to_latest():
    v = _inputs(
        finalized_ts=EPOCH_END - 100,
        finalized_snapshot=ROOT_B,
        latest_ts=EPOCH_END + SEQ_DELAY,
        latest_snapshot=ROOT_A,
    )
    assert decide(v) == Verdict.HONEST


def test_earliest_verification_mainnet_style():
    ts = earliest_verification_ts(1_000_000, PERIOD, SEQ_DELAY, 10_800, devnet=False)
    assert ts == 1_000_000 + PERIOD + SEQ_DELAY + 10_800


def test_earliest_verification_devnet_is_immediate():
    assert earliest_verification_ts(1_000_000, PERIOD, SEQ_DELAY, 10_800, devnet=True) == 1_000_000
