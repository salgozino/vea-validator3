"""Vea Claim struct: hashing parity with the Solidity contracts and reconstruction helpers.

The outbox contracts never store the Claim struct, only
``claimHashes[epoch] = keccak256(abi.encodePacked(...))``. Every state-changing
call takes the full struct and reverts unless its hash matches, so the bot must
reconstruct the struct from events and keep it current across transitions.

Two transitions are not directly observable and require *hash probing* (trying
candidate structs until one matches the on-chain hash):

- ``Verified(epoch)`` does not say who won: ``verifySnapshot`` sets
  ``honest = Claimer`` while ``resolveDisputedClaim`` may set either party.
- The escape-hatch withdrawals (bridge shutdown) zero out ``claimer`` or
  ``challenger`` without emitting any event.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Iterator

from eth_utils import keccak, to_canonical_address, to_checksum_address

ZERO_ADDRESS = "0x0000000000000000000000000000000000000000"
ZERO_ROOT = b"\x00" * 32


class Party(IntEnum):
    NONE = 0
    CLAIMER = 1
    CHALLENGER = 2


@dataclass(frozen=True)
class Claim:
    state_root: bytes  # 32 bytes
    claimer: str
    timestamp_claimed: int
    timestamp_verification: int = 0
    blocknumber_verification: int = 0
    honest: Party = Party.NONE
    challenger: str = ZERO_ADDRESS

    def __post_init__(self) -> None:
        if len(self.state_root) != 32:
            raise ValueError("state_root must be exactly 32 bytes")

    def hash(self) -> bytes:
        return hash_claim(self)

    def as_tuple(self) -> tuple:
        """Struct tuple in ABI order, for web3 contract calls."""
        return (
            self.state_root,
            to_checksum_address(self.claimer),
            self.timestamp_claimed % 2**32,
            self.timestamp_verification % 2**32,
            self.blocknumber_verification % 2**32,
            int(self.honest),
            to_checksum_address(self.challenger),
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "state_root": "0x" + self.state_root.hex(),
                "claimer": self.claimer,
                "timestamp_claimed": self.timestamp_claimed,
                "timestamp_verification": self.timestamp_verification,
                "blocknumber_verification": self.blocknumber_verification,
                "honest": int(self.honest),
                "challenger": self.challenger,
            }
        )

    @classmethod
    def from_json(cls, raw: str) -> "Claim":
        d = json.loads(raw)
        return cls(
            state_root=bytes.fromhex(d["state_root"].removeprefix("0x")),
            claimer=d["claimer"],
            timestamp_claimed=d["timestamp_claimed"],
            timestamp_verification=d["timestamp_verification"],
            blocknumber_verification=d["blocknumber_verification"],
            honest=Party(d["honest"]),
            challenger=d["challenger"],
        )


def hash_claim(c: Claim) -> bytes:
    """keccak256(abi.encodePacked(stateRoot, claimer, uint32 x3, uint8 honest, challenger)).

    85 packed bytes; must byte-for-byte match ``hashClaim`` in the outbox contracts.
    """
    packed = (
        c.state_root
        + to_canonical_address(c.claimer)
        + (c.timestamp_claimed % 2**32).to_bytes(4, "big")
        + (c.timestamp_verification % 2**32).to_bytes(4, "big")
        + (c.blocknumber_verification % 2**32).to_bytes(4, "big")
        + int(c.honest).to_bytes(1, "big")
        + to_canonical_address(c.challenger)
    )
    assert len(packed) == 85
    return keccak(packed)


def probe_variants(base: Claim) -> Iterator[Claim]:
    """Bounded mutation set covering every unobservable on-chain transition.

    Order matters: the base struct first (common case), then single-field
    mutations, then combinations.
    """
    seen: set[bytes] = set()
    for honest in (base.honest, Party.NONE, Party.CLAIMER, Party.CHALLENGER):
        for claimer in (base.claimer, ZERO_ADDRESS):
            for challenger in (base.challenger, ZERO_ADDRESS):
                candidate = replace(
                    base, honest=Party(honest), claimer=claimer, challenger=challenger
                )
                h = candidate.hash()
                if h not in seen:
                    seen.add(h)
                    yield candidate


def find_matching(base: Claim, onchain_hash: bytes) -> Claim | None:
    """Return the probe variant whose hash equals the on-chain claim hash, if any."""
    if onchain_hash == b"\x00" * 32:
        return None
    for candidate in probe_variants(base):
        if candidate.hash() == onchain_hash:
            return candidate
    return None
