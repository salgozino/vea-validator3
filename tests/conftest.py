"""Shared fakes for detector/challenger tests."""

from dataclasses import dataclass, field

import pytest

from vea_challenger.chain import QuorumError
from vea_challenger.claims import Claim, hash_claim
from vea_challenger.contracts import L2View, OutboxParams


@dataclass
class FakeNotifier:
    messages: list = field(default_factory=list)

    def send(self, level, message):
        self.messages.append((level, message))

    def has(self, level, needle=""):
        return any(l == level and needle in m for l, m in self.messages)


class FakeOutbox:
    """Stands in for OutboxContract: events + claim hashes + clocks."""

    def __init__(self):
        self.events: list[dict] = []
        self.claim_hashes: dict[int, bytes] = {}
        self.block_timestamps: dict[int, int] = {}
        self.head = 1_000
        self.confirmations = 3
        self._now = 0
        self.seq_delay = 86_400

    def confirmed_head(self):
        return self.head - self.confirmations

    def fetch_events(self, from_block, to_block, chunk):
        return [
            e for e in self.events if from_block <= e["block_number"] <= to_block
        ]

    def block_timestamp(self, n):
        return self.block_timestamps[n]

    def claim_hash(self, epoch):
        return self.claim_hashes.get(epoch, b"\x00" * 32)

    def now(self):
        return self._now

    def sequencer_delay_limit(self):
        return self.seq_delay


class FakeInbox:
    """snapshot_quorum stand-in with per-tag values."""

    def __init__(self):
        self.finalized: tuple[bytes, L2View] | None = None
        self.latest: tuple[bytes, L2View] | None = None
        self.quorum_split = False
        self.complete = True  # every configured RPC answered

    def snapshot_quorum(self, epoch, tag):
        if self.quorum_split:
            raise QuorumError("fake split")
        value = self.finalized if tag == "finalized" else self.latest
        if value is None:
            raise RuntimeError("no data")
        return (*value, self.complete)


@pytest.fixture
def params():
    return OutboxParams(
        deposit=10**18, epoch_period=7_200, min_challenge_period=10_800, timeout_epochs=10
    )
