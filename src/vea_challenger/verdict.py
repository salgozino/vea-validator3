"""Pure verdict logic: is a claim honest, fraudulent, or undecidable (yet)?

Safety rules (from the red-teamed design):

- A verdict may only come from inbox state that can no longer change:
  * the ``finalized`` tag once the finalized L2 timestamp passes the epoch end
    (finalized history cannot be reorged or backdated into), or
  * the ``latest`` tag once the latest L2 timestamp passes
    ``epoch end + sequencerDelayLimit`` — before that, a malicious sequencer can
    still backdate a ``saveSnapshot`` into the epoch, and N-of-M RPC agreement
    is no protection because every RPC mirrors the same sequencer feed.
- ``snapshots(epoch) == 0x0`` (nobody saved a snapshot) makes ANY claim for that
  epoch fraudulent, since resolution forwards the zero root.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Verdict(Enum):
    WAIT = "wait"  # cannot decide yet; keep polling
    HONEST = "honest"  # claimed root == immutable snapshot
    CHALLENGE = "challenge"  # claimed root != immutable snapshot


@dataclass(frozen=True)
class VerdictInputs:
    epoch: int
    claimed_root: bytes
    epoch_period: int
    sequencer_delay: int
    # Quorum reads of the inbox chain; None when the read failed / disagreed.
    finalized_ts: int | None
    finalized_snapshot: bytes | None
    latest_ts: int | None
    latest_snapshot: bytes | None

    @property
    def epoch_end(self) -> int:
        return (self.epoch + 1) * self.epoch_period


def decide(v: VerdictInputs) -> Verdict:
    # Primary: finalized data past the epoch boundary is immutable.
    if (
        v.finalized_snapshot is not None
        and v.finalized_ts is not None
        and v.finalized_ts >= v.epoch_end
    ):
        return Verdict.HONEST if v.finalized_snapshot == v.claimed_root else Verdict.CHALLENGE

    # Fallback for stalled finality: latest data older than the backdating window.
    if (
        v.latest_snapshot is not None
        and v.latest_ts is not None
        and v.latest_ts >= v.epoch_end + v.sequencer_delay
    ):
        return Verdict.HONEST if v.latest_snapshot == v.claimed_root else Verdict.CHALLENGE

    return Verdict.WAIT


def earliest_verification_ts(
    timestamp_claimed: int,
    epoch_period: int,
    sequencer_delay: int,
    min_challenge_period: int,
    devnet: bool,
) -> int:
    """Earliest outbox-chain time at which verifySnapshot can possibly succeed.

    This is the hard deadline for landing a challenge. Devnet outboxes skip both
    the sequencer-delay wait and the censorship test, so the deadline is
    effectively "immediately after the claim".
    """
    if devnet:
        return timestamp_claimed
    return timestamp_claimed + epoch_period + sequencer_delay + min_challenge_period
