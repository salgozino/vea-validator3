"""Claim hashing parity and reconstruction probing."""

from eth_utils import keccak

from vea_challenger.claims import (
    ZERO_ADDRESS,
    Claim,
    Party,
    find_matching,
    hash_claim,
    probe_variants,
)

CLAIMER = "0x00000000000000000000000000000000000000A1"
CHALLENGER = "0x00000000000000000000000000000000000000B2"


def _sample(**kw) -> Claim:
    base = dict(
        state_root=bytes.fromhex("11" * 32),
        claimer=CLAIMER,
        timestamp_claimed=1_700_000_001,
        timestamp_verification=0,
        blocknumber_verification=0,
        honest=Party.NONE,
        challenger=ZERO_ADDRESS,
    )
    base.update(kw)
    return Claim(**base)


def test_hash_claim_manual_packing():
    """Byte-for-byte reimplementation of Solidity abi.encodePacked."""
    c = _sample(
        timestamp_verification=1_700_000_002,
        blocknumber_verification=12_345,
        honest=Party.CLAIMER,
        challenger=CHALLENGER,
    )
    packed = (
        bytes.fromhex("11" * 32)
        + bytes.fromhex("00000000000000000000000000000000000000a1")
        + (1_700_000_001).to_bytes(4, "big")
        + (1_700_000_002).to_bytes(4, "big")
        + (12_345).to_bytes(4, "big")
        + b"\x01"
        + bytes.fromhex("00000000000000000000000000000000000000b2")
    )
    assert len(packed) == 85
    assert hash_claim(c) == keccak(packed)


def test_hash_claim_golden_vector():
    """Pinned digest: catches accidental encoding changes across refactors."""
    c = _sample()
    assert hash_claim(c).hex() == (
        keccak(
            bytes.fromhex("11" * 32)
            + bytes.fromhex("00000000000000000000000000000000000000a1")
            + (1_700_000_001).to_bytes(4, "big")
            + b"\x00" * 4
            + b"\x00" * 4
            + b"\x00"
            + b"\x00" * 20
        ).hex()
    )


def test_uint32_truncation():
    a = _sample(timestamp_claimed=2**32 + 5)
    b = _sample(timestamp_claimed=5)
    assert hash_claim(a) == hash_claim(b)


def test_state_root_must_be_32_bytes():
    import pytest

    with pytest.raises(ValueError):
        _sample(state_root=b"\x11" * 31)


def test_json_roundtrip():
    c = _sample(honest=Party.CHALLENGER, challenger=CHALLENGER)
    assert Claim.from_json(c.to_json()) == c


def test_as_tuple_matches_abi_order():
    c = _sample(honest=Party.CHALLENGER, challenger=CHALLENGER)
    t = c.as_tuple()
    assert t[0] == c.state_root
    assert t[5] == 2  # enum as int
    assert t[6].lower() == CHALLENGER.lower()


def test_probe_finds_verified_winner_challenger():
    """resolveDisputedClaim sets honest=Challenger without saying so in the event."""
    tracked = _sample(challenger=CHALLENGER)
    onchain = hash_claim(_sample(challenger=CHALLENGER, honest=Party.CHALLENGER))
    matched = find_matching(tracked, onchain)
    assert matched is not None
    assert matched.honest == Party.CHALLENGER


def test_probe_finds_escape_hatch_mutation():
    """withdrawClaimerEscapeHatch zeroes claimer with no event."""
    tracked = _sample(challenger=CHALLENGER)
    onchain = hash_claim(_sample(claimer=ZERO_ADDRESS, challenger=CHALLENGER))
    matched = find_matching(tracked, onchain)
    assert matched is not None
    assert matched.claimer == ZERO_ADDRESS
    assert matched.challenger == CHALLENGER


def test_probe_returns_none_for_unknown_hash():
    assert find_matching(_sample(), b"\xab" * 32) is None
    assert find_matching(_sample(), b"\x00" * 32) is None


def test_probe_variants_unique():
    variants = list(probe_variants(_sample(challenger=CHALLENGER)))
    hashes = [v.hash() for v in variants]
    assert len(hashes) == len(set(hashes))
    # base struct comes first (common case: no mutation)
    assert variants[0] == _sample(challenger=CHALLENGER)
