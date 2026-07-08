"""Multi-RPC chain client: rotating failover, quorum reads, tx submission.

Verdict-critical reads use ``read_quorum`` (every configured RPC must agree);
everything else rides a primary provider that rotates away on failure.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Iterable, TypeVar

from eth_account import Account
from eth_account.signers.local import LocalAccount
from web3 import HTTPProvider, Web3
from web3.types import TxReceipt

from .config import ChainCfg

log = logging.getLogger(__name__)

T = TypeVar("T")

FINALIZED = "finalized"
LATEST = "latest"


class QuorumError(RuntimeError):
    """Configured RPCs disagree on a verdict-critical read."""


class AllProvidersFailed(RuntimeError):
    pass


@dataclass
class Eip1559Fees:
    max_fee_per_gas: int
    max_priority_fee_per_gas: int


class ChainClient:
    def __init__(self, cfg: ChainCfg, request_timeout: float = 30.0):
        self.cfg = cfg
        self._w3s = [
            Web3(HTTPProvider(url, request_kwargs={"timeout": request_timeout}))
            for url in cfg.rpc_urls
        ]
        if not self._w3s:
            raise ValueError(f"chain {cfg.name}: no RPC URLs configured")
        self._primary = 0

    @property
    def name(self) -> str:
        return self.cfg.name

    @property
    def w3(self) -> Web3:
        return self._w3s[self._primary]

    def all_w3(self) -> list[Web3]:
        return list(self._w3s)

    def with_failover(self, fn: Callable[[Web3], T]) -> T:
        """Run fn against the primary; rotate to the next RPC on failure."""
        last_exc: Exception | None = None
        for attempt in range(len(self._w3s)):
            idx = (self._primary + attempt) % len(self._w3s)
            try:
                result = fn(self._w3s[idx])
                self._primary = idx
                return result
            except Exception as exc:  # noqa: BLE001 - provider errors are diverse
                last_exc = exc
                log.warning(
                    "%s: RPC %s failed: %s", self.cfg.name, self.cfg.rpc_urls[idx], exc
                )
        raise AllProvidersFailed(f"chain {self.cfg.name}: all RPCs failed") from last_exc

    def read_quorum(self, fn: Callable[[Web3], T]) -> T:
        """Run fn on every RPC; all reachable ones must agree, and at least one
        must answer. Raises QuorumError on disagreement."""
        results: list[T] = []
        errors: list[Exception] = []
        for w3, url in zip(self._w3s, self.cfg.rpc_urls):
            try:
                results.append(fn(w3))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)
                log.warning("%s: quorum read failed on %s: %s", self.cfg.name, url, exc)
        if not results:
            raise AllProvidersFailed(
                f"chain {self.cfg.name}: all RPCs failed during quorum read"
            ) from (errors[0] if errors else None)
        first = results[0]
        if any(r != first for r in results[1:]):
            raise QuorumError(
                f"chain {self.cfg.name}: RPCs disagree on quorum read: {results!r}"
            )
        return first

    # -- blocks / finality ----------------------------------------------------

    def block(self, tag: str | int = LATEST) -> Any:
        return self.with_failover(lambda w3: w3.eth.get_block(tag))

    def latest_block_number(self) -> int:
        return int(self.with_failover(lambda w3: w3.eth.block_number))

    def supports_finalized(self) -> bool:
        """True if the primary RPC serves a genuine `finalized` tag.

        An RPC that aliases finalized->latest returns the same block for both;
        real finality always lags the head on live chains (retry once to ride
        out the corner case of an idle chain tip).
        """
        for _ in range(2):
            try:
                fin = self.block(FINALIZED)
                latest = self.block(LATEST)
            except Exception:  # noqa: BLE001
                return False
            if fin["number"] < latest["number"]:
                return True
            time.sleep(2)
        return False

    # -- logs -----------------------------------------------------------------

    def get_logs_chunked(
        self,
        *,
        address: str,
        topics: list | None,
        from_block: int,
        to_block: int,
        chunk: int = 5_000,
        inter_chunk_delay: float = 0.25,
        retries: int = 4,
    ) -> list[dict]:
        out: list[dict] = []
        start = from_block
        while start <= to_block:
            end = min(start + chunk - 1, to_block)
            params = {
                "address": Web3.to_checksum_address(address),
                "fromBlock": start,
                "toBlock": end,
            }
            if topics is not None:
                params["topics"] = topics
            # Public RPCs rate-limit bursts of getLogs; back off and retry
            # before concluding the range is unfetchable.
            for attempt in range(retries):
                try:
                    out.extend(
                        self.with_failover(lambda w3, p=params: w3.eth.get_logs(p))
                    )
                    break
                except AllProvidersFailed:
                    if attempt == retries - 1:
                        raise
                    delay = 2.0 * 2**attempt
                    log.warning(
                        "%s: getLogs [%s, %s] rate-limited; retrying in %.0fs",
                        self.cfg.name,
                        start,
                        end,
                        delay,
                    )
                    time.sleep(delay)
            start = end + 1
            if start <= to_block and inter_chunk_delay:
                time.sleep(inter_chunk_delay)
        return out

    # -- transactions ---------------------------------------------------------

    def fees(self, urgency: float = 1.0) -> Eip1559Fees:
        def _fees(w3: Web3) -> Eip1559Fees:
            base = w3.eth.get_block(LATEST)["baseFeePerGas"]
            try:
                prio = w3.eth.max_priority_fee
            except Exception:  # noqa: BLE001 - some RPCs lack the endpoint
                prio = Web3.to_wei(0.1, "gwei")
            prio = max(int(prio * urgency), Web3.to_wei(0.05, "gwei"))
            return Eip1559Fees(
                max_fee_per_gas=int(base * 2 * urgency) + prio,
                max_priority_fee_per_gas=prio,
            )

        return self.with_failover(_fees)

    def pending_nonce(self, address: str) -> int:
        return int(
            self.with_failover(lambda w3: w3.eth.get_transaction_count(address, "pending"))
        )

    def send_signed(self, account: LocalAccount, tx: dict) -> str:
        signed = account.sign_transaction(tx)
        tx_hash = self.with_failover(
            lambda w3: w3.eth.send_raw_transaction(signed.raw_transaction)
        )
        return tx_hash.to_0x_hex()

    def wait_receipt(self, tx_hash: str, timeout: float = 300.0) -> TxReceipt:
        return self.with_failover(
            lambda w3: w3.eth.wait_for_transaction_receipt(tx_hash, timeout=timeout)
        )

    def receipt_or_none(self, tx_hash: str) -> TxReceipt | None:
        try:
            return self.with_failover(lambda w3: w3.eth.get_transaction_receipt(tx_hash))
        except AllProvidersFailed:
            raise
        except Exception:  # noqa: BLE001 - not found
            return None


def load_account(private_key: str) -> LocalAccount:
    return Account.from_key(private_key)


def wait_for(predicate: Callable[[], bool], timeout: float, interval: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()
