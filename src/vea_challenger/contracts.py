"""Typed wrappers around the Vea inbox/outbox contracts.

These adapt ChainClient + ABIs into the narrow read/write surface the detector,
challenger and resolver need. Verdict-critical inbox reads go through
``read_quorum`` so every configured RPC must agree.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from web3 import Web3

from . import abi
from .chain import FINALIZED, LATEST, ChainClient
from .claims import Claim
from .config import RouteCfg

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class OutboxParams:
    deposit: int
    epoch_period: int
    min_challenge_period: int
    timeout_epochs: int


@dataclass(frozen=True)
class L2View:
    """A consistent (timestamp, number) view of the inbox chain at some tag."""

    timestamp: int
    number: int


class OutboxContract:
    def __init__(self, client: ChainClient, route: RouteCfg):
        self.client = client
        self.route = route
        self.address = Web3.to_checksum_address(route.outbox_address)
        self._contract_for = {
            id(w3): w3.eth.contract(address=self.address, abi=abi.OUTBOX_ABI)
            for w3 in client.all_w3()
        }

    def _c(self, w3: Web3):
        return self._contract_for[id(w3)]

    def contract(self):
        return self._c(self.client.w3)

    # -- reads ----------------------------------------------------------------

    def params(self) -> OutboxParams:
        def _read(w3: Web3) -> OutboxParams:
            c = self._c(w3)
            return OutboxParams(
                deposit=c.functions.deposit().call(),
                epoch_period=c.functions.epochPeriod().call(),
                min_challenge_period=c.functions.minChallengePeriod().call(),
                timeout_epochs=c.functions.timeoutEpochs().call(),
            )

        return self.client.with_failover(_read)

    def sequencer_delay_limit(self) -> int:
        return int(
            self.client.with_failover(
                lambda w3: self._c(w3).functions.sequencerDelayLimit().call()
            )
        )

    def claim_hash(self, epoch: int) -> bytes:
        return bytes(
            self.client.with_failover(
                lambda w3: self._c(w3).functions.claimHashes(epoch).call()
            )
        )

    def latest_verified_epoch(self) -> int:
        return int(
            self.client.with_failover(
                lambda w3: self._c(w3).functions.latestVerifiedEpoch().call()
            )
        )

    def hash_claim_onchain(self, claim: Claim) -> bytes:
        """Contract-side hashClaim; used once at startup for parity checking."""
        return bytes(
            self.client.with_failover(
                lambda w3: self._c(w3).functions.hashClaim(claim.as_tuple()).call()
            )
        )

    def fetch_events(self, from_block: int, to_block: int, chunk: int) -> list[dict]:
        """All outbox events in range, normalized and ordered by (block, logIndex)."""
        raw = self.client.get_logs_chunked(
            address=self.address,
            topics=None,
            from_block=from_block,
            to_block=to_block,
            chunk=chunk,
        )
        contract = self.contract()
        events = []
        by_topic = {
            contract.events[name]().topic: name
            for name in (
                "Claimed",
                "Challenged",
                "VerificationStarted",
                "Verified",
                "FailedResolution",
            )
        }
        for entry in raw:
            topic0 = entry["topics"][0].to_0x_hex() if entry["topics"] else None
            name = by_topic.get(topic0)
            if name is None:
                continue
            parsed = contract.events[name]().process_log(entry)
            events.append(
                {
                    "name": name,
                    "args": dict(parsed["args"]),
                    "block_number": parsed["blockNumber"],
                    "log_index": parsed["logIndex"],
                    "tx_hash": parsed["transactionHash"].to_0x_hex(),
                }
            )
        events.sort(key=lambda e: (e["block_number"], e["log_index"]))
        return events

    @lru_cache(maxsize=4096)
    def block_timestamp(self, block_number: int) -> int:
        return int(self.client.block(block_number)["timestamp"])

    def now(self) -> int:
        """Outbox-chain clock (latest block timestamp)."""
        return int(self.client.block(LATEST)["timestamp"])

    def confirmed_head(self) -> int:
        return self.client.latest_block_number() - self.route.outbox_chain.confirmations


class InboxContract:
    def __init__(self, client: ChainClient, route: RouteCfg):
        self.client = client
        self.route = route
        self.address = Web3.to_checksum_address(route.inbox_address)

    def _contract(self, w3: Web3):
        return w3.eth.contract(address=self.address, abi=abi.INBOX_ABI)

    def snapshot_quorum(self, epoch: int, tag: str) -> tuple[bytes, L2View]:
        """(snapshots(epoch), block view) at `tag`, agreed by every configured RPC.

        The block is pinned by number on each provider so the snapshot value and
        the view refer to the same height per provider; providers must agree on
        the snapshot value. The returned view is the *minimum* timestamp across
        providers (conservative for "has the epoch ended" checks).
        """

        def _read(w3: Web3) -> tuple[bytes, int, int]:
            block = w3.eth.get_block(tag)
            root = self._contract(w3).functions.snapshots(epoch).call(
                block_identifier=block["number"]
            )
            return (bytes(root), int(block["timestamp"]), int(block["number"]))

        results = []
        for w3 in self.client.all_w3():
            try:
                results.append(_read(w3))
            except Exception as exc:  # noqa: BLE001
                log.warning("inbox quorum read failed: %s", exc)
        if not results:
            raise RuntimeError("inbox: all RPCs failed reading snapshot")
        roots = {r[0] for r in results}
        if len(roots) != 1:
            from .chain import QuorumError

            raise QuorumError(f"inbox RPCs disagree on snapshots({epoch}): {roots!r}")
        min_ts = min(r[1] for r in results)
        min_num = min(r[2] for r in results)
        return results[0][0], L2View(timestamp=min_ts, number=min_num)

    def l2_view(self, tag: str) -> L2View:
        block = self.client.block(tag)
        return L2View(timestamp=int(block["timestamp"]), number=int(block["number"]))

    def send_snapshot_fn(self, epoch: int, claim: Claim) -> Any:
        """Bound contract function for sendSnapshot, per route kind."""
        w3 = self.client.w3
        if self.route.kind == "arb_to_gnosis":
            c = w3.eth.contract(
                address=self.address, abi=abi.INBOX_GNOSIS_SEND_SNAPSHOT_ABI
            )
            return c.functions.sendSnapshot(
                epoch, self.route.amb_gas_limit, claim.as_tuple()
            )
        c = self._contract(w3)
        return c.functions.sendSnapshot(epoch, claim.as_tuple())

    def snapshot_sent_events(self, epoch: int, from_block: int, to_block: int) -> list[dict]:
        contract = self._contract(self.client.w3)
        event = contract.events.SnapshotSent()
        raw = self.client.get_logs_chunked(
            address=self.address,
            topics=[event.topic, "0x" + epoch.to_bytes(32, "big").hex()],
            from_block=from_block,
            to_block=to_block,
            chunk=100_000,
        )
        return [dict(event.process_log(entry)["args"]) | {
            "tx_hash": entry["transactionHash"].to_0x_hex()
        } for entry in raw]


class WethToken:
    def __init__(self, client: ChainClient, token_address: str):
        self.client = client
        self.address = Web3.to_checksum_address(token_address)

    def _contract(self, w3: Web3):
        return w3.eth.contract(address=self.address, abi=abi.WETH_ABI)

    def balance_of(self, owner: str) -> int:
        return int(
            self.client.with_failover(
                lambda w3: self._contract(w3).functions.balanceOf(owner).call()
            )
        )

    def allowance(self, owner: str, spender: str) -> int:
        return int(
            self.client.with_failover(
                lambda w3: self._contract(w3).functions.allowance(owner, spender).call()
            )
        )

    def approve_fn(self, spender: str, amount: int) -> Any:
        return self._contract(self.client.w3).functions.approve(spender, amount)
