"""Event sync, claim-struct reconstruction, and verdict evaluation.

The detector scans outbox events into the SQLite state machine, keeps each
tracked Claim struct byte-identical to the on-chain ``claimHashes`` preimage
(including probing for the event-less escape-hatch mutations and the ambiguous
``Verified`` winner), and evaluates verdicts against quorum inbox reads.
"""

from __future__ import annotations

import logging

from .chain import FINALIZED, LATEST, QuorumError
from .claims import ZERO_ADDRESS, Claim, Party, find_matching
from .contracts import InboxContract, OutboxContract, OutboxParams
from .notify import CRITICAL, INFO, WARNING, Notifier
from .store import (
    CHALLENGE_PENDING,
    CHALLENGED,
    CHALLENGED_BY_OTHER,
    DESYNC,
    HONEST,
    L1_EXECUTED,
    LOST,
    MISSED,
    RESOLVED,
    SEEN,
    SNAPSHOT_SENT,
    UNDECIDED,
    WITHDRAWN,
    ClaimRow,
    Store,
)
from .verdict import Verdict, VerdictInputs, decide

log = logging.getLogger(__name__)

_OUR_CHALLENGE_STATES = (CHALLENGE_PENDING, CHALLENGED, SNAPSHOT_SENT, L1_EXECUTED)


class Detector:
    def __init__(
        self,
        store: Store,
        outbox: OutboxContract,
        inbox: InboxContract,
        params: OutboxParams,
        notifier: Notifier,
        challenge_address: str | None,
        devnet: bool,
        log_scan_chunk: int = 5_000,
        genesis_lookback_blocks: int = 200_000,
    ):
        self.store = store
        self.outbox = outbox
        self.inbox = inbox
        self.params = params
        self.notifier = notifier
        self.challenge_address = challenge_address
        self.devnet = devnet
        self.log_scan_chunk = log_scan_chunk
        self.genesis_lookback_blocks = genesis_lookback_blocks

    # -- event sync ------------------------------------------------------------

    def sync_events(self) -> None:
        head = self.outbox.confirmed_head()
        cursor = self.store.get_cursor("outbox_events")
        if cursor is None:
            cursor = max(0, head - self.genesis_lookback_blocks)
        if head <= cursor:
            return
        events = self.outbox.fetch_events(cursor + 1, head, self.log_scan_chunk)
        for ev in events:
            try:
                self._apply_event(ev)
            except Exception:  # noqa: BLE001
                # Do NOT skip past a failed event: a dropped Claimed is a fraud
                # the bot would never track. Rewind the cursor to just before it
                # and retry the range next tick (all handlers are idempotent).
                log.exception("failed applying event %s", ev)
                self.notifier.send(
                    CRITICAL, f"event processing failed (will retry next tick): {ev}"
                )
                self.store.set_cursor("outbox_events", ev["block_number"] - 1)
                return
        self.store.set_cursor("outbox_events", head)

    def _apply_event(self, ev: dict) -> None:
        name, args = ev["name"], ev["args"]
        if name == "Claimed":
            epoch = int(args["_epoch"])
            ts = self.outbox.block_timestamp(ev["block_number"])
            claim = Claim(
                state_root=bytes(args["_stateRoot"]),
                claimer=args["_claimer"],
                timestamp_claimed=ts,
            )
            existing = self.store.get_claim(epoch)
            if existing is not None:
                return  # duplicate scan
            self.store.upsert_claim(ClaimRow(epoch=epoch, status=SEEN, claim=claim))
            self.notifier.send(
                INFO,
                f"claim seen: epoch={epoch} claimer={claim.claimer} "
                f"root=0x{claim.state_root.hex()}",
            )
            return

        epoch = int(args["_epoch"]) if "_epoch" in args else int(args.get("_epochSent", -1))
        row = self.store.get_claim(epoch)
        if row is None:
            return  # event for a claim from before our lookback window

        if name == "VerificationStarted":
            ts = self.outbox.block_timestamp(ev["block_number"])
            row.claim = Claim(
                state_root=row.claim.state_root,
                claimer=row.claim.claimer,
                timestamp_claimed=row.claim.timestamp_claimed,
                timestamp_verification=ts,
                blocknumber_verification=ev["block_number"],
                honest=row.claim.honest,
                challenger=row.claim.challenger,
            )
            self.store.upsert_claim(row)

        elif name == "Challenged":
            challenger = args["_challenger"]
            row.claim = Claim(
                state_root=row.claim.state_root,
                claimer=row.claim.claimer,
                timestamp_claimed=row.claim.timestamp_claimed,
                timestamp_verification=row.claim.timestamp_verification,
                blocknumber_verification=row.claim.blocknumber_verification,
                honest=row.claim.honest,
                challenger=challenger,
            )
            ours = (
                self.challenge_address is not None
                and challenger.lower() == self.challenge_address.lower()
            )
            # Only transition rows that haven't advanced past the challenge
            # stage: a rescan of an old Challenged event must not drag a
            # SNAPSHOT_SENT/RESOLVED row backwards.
            pre_challenge = row.status in (SEEN, UNDECIDED, CHALLENGE_PENDING)
            if ours:
                if pre_challenge:
                    row.status = CHALLENGED
                row.challenge_tx = row.challenge_tx or ev["tx_hash"]
                self.notifier.send(INFO, f"our challenge confirmed for epoch {epoch}")
            elif pre_challenge:
                # Includes CHALLENGE_PENDING: a third party front-ran our
                # challenge; stand down (the fraud is handled either way).
                row.status = CHALLENGED_BY_OTHER
                self.notifier.send(
                    WARNING, f"epoch {epoch} challenged by third party {challenger}"
                )
            self.store.upsert_claim(row)

        elif name == "Verified":
            self._apply_verified(row)

        elif name == "FailedResolution":
            if row.status in (SNAPSHOT_SENT, L1_EXECUTED):
                row.status = CHALLENGED
                row.snapshot_tx = None
                row.l2tol1_json = None
                row.execute_tx = None
                row.note = "FailedResolution: stale struct sent; will re-send snapshot"
                self.store.upsert_claim(row)
                self.notifier.send(
                    CRITICAL,
                    f"resolution FAILED for epoch {epoch} (stale claim struct); re-sending",
                )

    def _apply_verified(self, row: ClaimRow) -> None:
        """`Verified` does not say who won — probe the on-chain hash."""
        onchain = self.outbox.claim_hash(row.epoch)
        if onchain == b"\x00" * 32:
            # Verified and already withdrawn before we processed the event.
            # The withdrawal paid claim.challenger / claim.claimer directly, so
            # funds are safe either way; flag for a human to confirm which.
            row.status = WITHDRAWN
            row.note = "verified + withdrawn before processing; verify balances"
            self.store.upsert_claim(row)
            self.notifier.send(
                WARNING,
                f"epoch {row.epoch}: verified and withdrawn in one scan window; "
                "check whether the challenge won (balances)",
            )
            return
        matched = find_matching(row.claim, onchain)
        if matched is None:
            row.status = DESYNC
            row.note = "Verified event but no probe variant matches claimHashes"
            self.store.upsert_claim(row)
            self.notifier.send(CRITICAL, f"epoch {row.epoch}: DESYNC after Verified")
            return
        row.claim = matched
        ours = (
            self.challenge_address is not None
            and matched.challenger.lower() == self.challenge_address.lower()
        )
        if matched.honest == Party.CHALLENGER:
            if ours:
                row.status = RESOLVED
                self.notifier.send(
                    INFO, f"epoch {row.epoch}: challenge WON, withdrawal pending"
                )
            else:
                row.status = MISSED
                row.note = "fraud defeated by another challenger"
        elif matched.honest == Party.CLAIMER:
            if row.status in _OUR_CHALLENGE_STATES:
                row.status = LOST
                self.notifier.send(
                    CRITICAL,
                    f"epoch {row.epoch}: our challenge LOST — deposit forfeited. "
                    "Investigate immediately (RPC lied or verdict bug).",
                )
            else:
                if row.status not in (HONEST,):
                    row.note = row.note or "verified as honest"
                row.status = HONEST
        self.store.upsert_claim(row)

    # -- struct reconciliation ---------------------------------------------------

    def reconcile(self, row: ClaimRow) -> ClaimRow | None:
        """Ensure our struct hashes to the on-chain claimHashes[epoch].

        Returns the (possibly updated) row, or None when the claim hash is gone
        (deleted by a withdrawal).
        """
        onchain = self.outbox.claim_hash(row.epoch)
        if onchain == b"\x00" * 32:
            if row.status == RESOLVED:
                # Someone executed withdrawChallengeDeposit for us; funds went to
                # claim.challenger regardless of caller.
                row.status = WITHDRAWN
                self.store.upsert_claim(row)
                self.notifier.send(INFO, f"epoch {row.epoch}: deposit withdrawn")
                return row
            row.status = WITHDRAWN if row.status == HONEST else row.status
            self.store.upsert_claim(row)
            return None
        if row.claim.hash() == onchain:
            return row
        matched = find_matching(row.claim, onchain)
        if matched is None:
            row.status = DESYNC
            row.note = "claimHashes changed to an unreconstructable value"
            self.store.upsert_claim(row)
            self.notifier.send(
                CRITICAL,
                f"epoch {row.epoch}: cannot reconstruct on-chain claim hash "
                f"({onchain.hex()}); manual intervention required",
            )
            return row
        row.claim = matched
        row.note = "struct updated via hash probing (event-less mutation)"
        self.store.upsert_claim(row)
        return row

    # -- verdicts ----------------------------------------------------------------

    def evaluate(self, row: ClaimRow) -> Verdict:
        """Verdict for a SEEN/UNDECIDED claim from quorum inbox reads."""
        finalized_snapshot = finalized_ts = None
        latest_snapshot = latest_ts = None
        complete = True  # every configured RPC answered every read we used
        try:
            snap, view, full = self.inbox.snapshot_quorum(row.epoch, FINALIZED)
            finalized_snapshot, finalized_ts = snap, view.timestamp
            complete = complete and full
        except QuorumError as exc:
            self.notifier.send(CRITICAL, f"epoch {row.epoch}: finalized quorum split: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.warning("finalized read failed for epoch %s: %s", row.epoch, exc)
        try:
            snap, view, full = self.inbox.snapshot_quorum(row.epoch, LATEST)
            latest_snapshot, latest_ts = snap, view.timestamp
            complete = complete and full
        except QuorumError as exc:
            self.notifier.send(CRITICAL, f"epoch {row.epoch}: latest quorum split: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.warning("latest read failed for epoch %s: %s", row.epoch, exc)

        verdict = decide(
            VerdictInputs(
                epoch=row.epoch,
                claimed_root=row.claim.state_root,
                epoch_period=self.params.epoch_period,
                sequencer_delay=self.outbox.sequencer_delay_limit(),
                finalized_ts=finalized_ts,
                finalized_snapshot=finalized_snapshot,
                latest_ts=latest_ts,
                latest_snapshot=latest_snapshot,
            )
        )

        if verdict == Verdict.HONEST and not complete:
            # HONEST is irreversible (we stop tracking): it requires agreement
            # from every configured RPC, not just the reachable ones.
            self.notifier.send(
                WARNING,
                f"epoch {row.epoch}: looks honest but not all RPCs answered; "
                "withholding the terminal verdict",
            )
            verdict = Verdict.WAIT

        if verdict == Verdict.HONEST:
            row.status = HONEST
            row.true_root = "0x" + (finalized_snapshot or latest_snapshot).hex()
            self.store.upsert_claim(row)
        elif verdict == Verdict.CHALLENGE:
            row.true_root = "0x" + (finalized_snapshot or latest_snapshot or b"").hex()
            self.store.upsert_claim(row)
        elif row.status == SEEN:
            row.status = UNDECIDED
            self.store.upsert_claim(row)
        return verdict
