"""Journaled transaction submission.

Intent is written to SQLite *before* broadcast so a crash mid-send is
recoverable; receipts finalize the journal entry. Each lane (challenge vs ops)
uses its own account so a stuck slow-lane tx can never block a challenge.
"""

from __future__ import annotations

import logging
from typing import Any

from eth_account.signers.local import LocalAccount
from web3.types import TxReceipt

from .chain import ChainClient
from .store import Store

log = logging.getLogger(__name__)


class TxFailed(RuntimeError):
    def __init__(self, tx_hash: str, receipt: TxReceipt):
        super().__init__(f"tx {tx_hash} reverted")
        self.tx_hash = tx_hash
        self.receipt = receipt


class TxSender:
    def __init__(self, store: Store, client: ChainClient, account: LocalAccount):
        self.store = store
        self.client = client
        self.account = account

    @property
    def address(self) -> str:
        return self.account.address

    def send(
        self,
        fn: Any,
        *,
        epoch: int,
        action: str,
        value: int = 0,
        urgency: float = 1.0,
        gas_limit: int | None = None,
        timeout: float = 300.0,
    ) -> TxReceipt:
        """Simulate, journal, broadcast and await one contract call."""
        tx_params: dict[str, Any] = {"from": self.account.address, "value": value}
        # eth_call simulation first: never broadcast a tx that reverts.
        fn.call(tx_params)

        fees = self.client.fees(urgency)
        nonce = self.client.pending_nonce(self.account.address)
        tx_params |= {
            "nonce": nonce,
            "maxFeePerGas": fees.max_fee_per_gas,
            "maxPriorityFeePerGas": fees.max_priority_fee_per_gas,
            "chainId": self.client.cfg.chain_id,
        }
        if gas_limit is not None:
            tx_params["gas"] = gas_limit
        tx = fn.build_transaction(tx_params)
        if gas_limit is None:
            tx["gas"] = int(tx["gas"] * 1.3)

        journal_id = self.store.journal_intent(
            epoch, action, self.client.name, self.account.address, nonce
        )
        tx_hash = self.client.send_signed(self.account, tx)
        self.store.journal_sent(journal_id, tx_hash)
        log.info("%s epoch=%s tx=%s nonce=%s sent", action, epoch, tx_hash, nonce)

        receipt = self.client.wait_receipt(tx_hash, timeout=timeout)
        if receipt["status"] != 1:
            self.store.journal_final(journal_id, "REVERTED")
            raise TxFailed(tx_hash, receipt)
        self.store.journal_final(journal_id, "CONFIRMED")
        return receipt
