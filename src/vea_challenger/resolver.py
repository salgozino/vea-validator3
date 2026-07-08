"""Drives challenged claims to resolution and collects the reward.

Slow lane (ops account): nothing here is deadline-critical, but each step must
be idempotent and crash-safe:

  CHALLENGED      -> sendSnapshot on the inbox (L2), with the exact
                     post-challenge struct; adopt a third party's ticket only
                     if their calldata carried the same struct
  SNAPSHOT_SENT   -> execute the L2->L1 message on the L1 Arbitrum Outbox once
                     the assertion confirms (ArbToGnosis: this executes
                     route(), the AMB then delivers to the Gnosis outbox)
  RESOLVED        -> withdrawChallengeDeposit on the outbox chain
  bridge shutdown -> withdrawChallengerEscapeHatch (deposit only, no reward)
"""

from __future__ import annotations

import json
import logging
import time

from .arbitrum import (
    ArbOutboxExecutor,
    NotReadyError,
    decode_claim_from_l2tol1_data,
    parse_l2tol1_event,
)
from .claims import Party
from .contracts import InboxContract, OutboxContract, OutboxParams
from .notify import CRITICAL, INFO, WARNING, Notifier
from .store import (
    CHALLENGED,
    L1_EXECUTED,
    RESOLVED,
    SNAPSHOT_SENT,
    WITHDRAWN,
    ClaimRow,
    Store,
)
from .txsender import TxSender

log = logging.getLogger(__name__)


class Resolver:
    def __init__(
        self,
        store: Store,
        outbox: OutboxContract,
        inbox: InboxContract,
        params: OutboxParams,
        executor: ArbOutboxExecutor,
        l2_sender: TxSender,  # ops key on the inbox (Arbitrum) chain
        l1_sender: TxSender,  # ops key on the L1 chain (rollup home)
        outbox_sender: TxSender,  # ops key on the outbox chain
        notifier: Notifier,
        challenge_address: str,
        dry_run: bool,
    ):
        self.store = store
        self.outbox = outbox
        self.inbox = inbox
        self.params = params
        self.executor = executor
        self.l2_sender = l2_sender
        self.l1_sender = l1_sender
        self.outbox_sender = outbox_sender
        self.notifier = notifier
        self.challenge_address = challenge_address
        self.dry_run = dry_run

    def tick(self, rows: list[ClaimRow]) -> None:
        if self.dry_run:
            return
        for row in rows:
            try:
                if row.status == CHALLENGED and self._ours(row):
                    self.send_snapshot(row)
                elif row.status == SNAPSHOT_SENT:
                    self.execute_l1(row)
                elif row.status == RESOLVED:
                    self.withdraw(row)
            except Exception as exc:  # noqa: BLE001 - resolution retries next tick
                log.warning("resolver step failed for epoch %s: %s", row.epoch, exc)

    def _ours(self, row: ClaimRow) -> bool:
        return row.claim.challenger.lower() == self.challenge_address.lower()

    # -- step 1: sendSnapshot ---------------------------------------------------

    def send_snapshot(self, row: ClaimRow) -> None:
        # Adopt an existing ticket if a third party already sent the snapshot
        # with the exact same (correct) struct.
        adopted = self._find_existing_ticket(row)
        if adopted is not None:
            row.snapshot_tx, row.l2tol1_json = adopted
            row.status = SNAPSHOT_SENT
            self.store.upsert_claim(row)
            self.notifier.send(INFO, f"epoch {row.epoch}: adopted existing snapshot ticket")
            return

        fn = self.inbox.send_snapshot_fn(row.epoch, row.claim)
        receipt = self.l2_sender.send(fn, epoch=row.epoch, action="send_snapshot")
        msg = parse_l2tol1_event(self.inbox.client, receipt)
        row.snapshot_tx = receipt["transactionHash"].to_0x_hex()
        row.l2tol1_json = json.dumps(msg)
        row.status = SNAPSHOT_SENT
        self.store.upsert_claim(row)
        self.notifier.send(
            INFO,
            f"epoch {row.epoch}: snapshot sent through canonical bridge "
            f"(tx {row.snapshot_tx}, position {msg['position']})",
        )

    def _find_existing_ticket(self, row: ClaimRow) -> tuple[str, str] | None:
        """Scan SnapshotSent events for this epoch; verify the struct they carried."""
        try:
            head = self.inbox.client.latest_block_number()
            events = self.inbox.snapshot_sent_events(
                row.epoch, max(0, head - 2_000_000), head
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("SnapshotSent scan failed: %s", exc)
            return None
        for ev in events:
            try:
                receipt = self.inbox.client.wait_receipt(ev["tx_hash"], timeout=30)
                msg = parse_l2tol1_event(self.inbox.client, receipt)
                carried = decode_claim_from_l2tol1_data(msg["data"])
                if carried is not None and carried.hash() == row.claim.hash():
                    return ev["tx_hash"], json.dumps(msg)
            except Exception as exc:  # noqa: BLE001
                log.warning("could not verify SnapshotSent ticket %s: %s", ev, exc)
        return None

    # -- step 2: L1 execution -----------------------------------------------------

    def execute_l1(self, row: ClaimRow) -> None:
        if not row.l2tol1_json:
            # Crash window: snapshot sent but message not journaled. Re-derive.
            if row.snapshot_tx:
                receipt = self.inbox.client.wait_receipt(row.snapshot_tx, timeout=30)
                row.l2tol1_json = json.dumps(parse_l2tol1_event(self.inbox.client, receipt))
                self.store.upsert_claim(row)
            else:
                row.status = CHALLENGED  # re-run step 1
                self.store.upsert_claim(row)
                return
        msg = json.loads(row.l2tol1_json)

        if self.executor.is_spent(msg["position"]):
            row.status = L1_EXECUTED
            self.store.upsert_claim(row)
            self.notifier.send(INFO, f"epoch {row.epoch}: L2->L1 message executed")
            return
        try:
            fn = self.executor.execute_fn(msg)
        except NotReadyError as exc:
            log.info("epoch %s: %s", row.epoch, exc)
            return
        receipt = self.l1_sender.send(
            fn, epoch=row.epoch, action="execute_l2_to_l1", timeout=600
        )
        row.execute_tx = receipt["transactionHash"].to_0x_hex()
        row.status = L1_EXECUTED
        self.store.upsert_claim(row)
        self.notifier.send(
            INFO, f"epoch {row.epoch}: executed L2->L1 message (tx {row.execute_tx})"
        )
        # ArbToEth: resolveDisputedClaim already ran inside this tx; the detector
        # picks up Verified on the next sync. ArbToGnosis: the AMB leg follows.

    # -- step 3: withdraw ----------------------------------------------------------

    def withdraw(self, row: ClaimRow) -> None:
        if row.claim.honest != Party.CHALLENGER or not self._ours(row):
            return
        fn = self.outbox.contract().functions.withdrawChallengeDeposit(
            row.epoch, row.claim.as_tuple()
        )
        receipt = self.outbox_sender.send(fn, epoch=row.epoch, action="withdraw")
        row.withdraw_tx = receipt["transactionHash"].to_0x_hex()
        row.status = WITHDRAWN
        self.store.upsert_claim(row)
        self.notifier.send(
            INFO,
            f"epoch {row.epoch}: reward withdrawn to {row.claim.challenger} "
            f"(tx {row.withdraw_tx})",
        )

    # -- shutdown escape hatch --------------------------------------------------------

    def bridge_shutdown(self) -> bool:
        now_epoch = self.outbox.now() // self.params.epoch_period
        return now_epoch - self.outbox.latest_verified_epoch() > self.params.timeout_epochs

    def escape_hatch(self, row: ClaimRow) -> None:
        if self.dry_run or not self._ours(row):
            return
        fn = self.outbox.contract().functions.withdrawChallengerEscapeHatch(
            row.epoch, row.claim.as_tuple()
        )
        receipt = self.outbox_sender.send(fn, epoch=row.epoch, action="escape_hatch")
        row.withdraw_tx = receipt["transactionHash"].to_0x_hex()
        row.status = WITHDRAWN
        row.note = "bridge shutdown: deposit recovered via escape hatch (no reward)"
        self.store.upsert_claim(row)
        self.notifier.send(WARNING, f"epoch {row.epoch}: escape hatch used (bridge shutdown)")
