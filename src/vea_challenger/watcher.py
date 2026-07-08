"""Main orchestration: wiring, startup self-checks, and the polling loop."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from eth_account import Account

from .arbitrum import ArbOutboxExecutor
from .chain import ChainClient
from .challenger import Challenger
from .claims import Claim, ZERO_ADDRESS
from .config import RouteCfg, Settings
from .contracts import InboxContract, OutboxContract, WethToken
from .detector import Detector
from .notify import CRITICAL, INFO, WARNING, Notifier
from .resolver import Resolver
from .store import (
    CHALLENGE_PENDING,
    CHALLENGED,
    L1_EXECUTED,
    RESOLVED,
    SEEN,
    SNAPSHOT_SENT,
    UNDECIDED,
    Store,
)
from .txsender import TxSender
from .verdict import Verdict

log = logging.getLogger(__name__)


@dataclass
class App:
    settings: Settings
    route: RouteCfg
    store: Store
    notifier: Notifier
    outbox: OutboxContract
    inbox: InboxContract
    detector: Detector
    challenger: Challenger | None
    resolver: Resolver | None


def build_app(settings: Settings) -> App:
    route = settings.resolve_route()
    store = Store(settings.resolve_db_path())
    notifier = Notifier(settings.webhook_url, route.name)

    outbox_client = ChainClient(route.outbox_chain)
    inbox_client = ChainClient(route.inbox_chain)
    l1_client = (
        outbox_client if route.kind == "arb_to_eth" else ChainClient(route.l1_chain)
    )

    outbox = OutboxContract(outbox_client, route)
    inbox = InboxContract(inbox_client, route)
    params = outbox.params()

    challenge_account = ops_account = None
    if settings.challenge_private_key is not None:
        challenge_account = Account.from_key(
            settings.challenge_private_key.get_secret_value()
        )
        ops_account = challenge_account
    if settings.ops_private_key is not None:
        ops_account = Account.from_key(settings.ops_private_key.get_secret_value())
        if challenge_account is None:
            challenge_account = ops_account
    if (
        challenge_account is not None
        and ops_account is not None
        and challenge_account.address == ops_account.address
    ):
        log.warning(
            "single key configured: slow resolution txs share a nonce lane with "
            "deadline-critical challenges. Set both VEA_CHALLENGE_PRIVATE_KEY and "
            "VEA_OPS_PRIVATE_KEY (different keys) for separate lanes."
        )

    detector = Detector(
        store,
        outbox,
        inbox,
        params,
        notifier,
        challenge_account.address if challenge_account else None,
        route.devnet,
        settings.log_scan_chunk,
        settings.genesis_lookback_blocks,
    )

    challenger = resolver = None
    if challenge_account is not None:
        weth = (
            WethToken(outbox_client, route.weth_address)
            if route.weth_address
            else None
        )
        challenger = Challenger(
            store,
            outbox,
            params,
            TxSender(store, outbox_client, challenge_account),
            notifier,
            weth,
            settings.gas_reserve_wei,
            route.devnet,
            settings.dry_run,
        )
        executor = ArbOutboxExecutor(
            l1_client, inbox_client, route.arb_bridge_address, route.arb_outbox_address
        )
        resolver = Resolver(
            store,
            outbox,
            inbox,
            params,
            executor,
            TxSender(store, inbox_client, ops_account),
            TxSender(store, l1_client, ops_account),
            TxSender(store, outbox_client, ops_account),
            notifier,
            challenge_account.address,
            settings.dry_run,
        )

    return App(
        settings=settings,
        route=route,
        store=store,
        notifier=notifier,
        outbox=outbox,
        inbox=inbox,
        detector=detector,
        challenger=challenger,
        resolver=resolver,
    )


def startup_checks(app: App) -> None:
    """Fail fast on anything that would silently corrupt verdicts later."""
    route = app.route

    if len(route.inbox_chain.rpc_urls) < 2:
        raise SystemExit(
            "verdicts require at least 2 independent inbox-chain RPCs "
            "(set VEA_INBOX_RPC_URLS with two or more providers)"
        )

    for client, label in ((app.inbox.client, "inbox"), (app.outbox.client, "outbox")):
        cid = client.with_failover(lambda w3: w3.eth.chain_id)
        expected = route.inbox_chain.chain_id if label == "inbox" else route.outbox_chain.chain_id
        if cid != expected:
            raise SystemExit(f"{label} RPC chain id {cid} != configured {expected}")

    if not app.inbox.client.supports_finalized():
        raise SystemExit(
            "inbox chain RPC does not serve a genuine 'finalized' tag; "
            "verdicts would be unsafe. Configure an RPC with finality support."
        )

    # hashClaim parity: local implementation vs contract, on a synthetic struct.
    probe = Claim(
        state_root=b"\x11" * 32,
        claimer="0x00000000000000000000000000000000000000A1",
        timestamp_claimed=1_700_000_001,
        timestamp_verification=1_700_000_002,
        blocknumber_verification=12_345,
        honest=1,
        challenger="0x00000000000000000000000000000000000000B2",
    )
    onchain = app.outbox.hash_claim_onchain(probe)
    if onchain != probe.hash():
        raise SystemExit(
            "hashClaim parity check FAILED: local packed encoding differs from the "
            f"contract ({onchain.hex()} != {probe.hash().hex()})"
        )

    params = app.outbox.params()
    log.info(
        "route %s: deposit=%s wei epochPeriod=%ss minChallengePeriod=%ss "
        "sequencerDelayLimit=%ss devnet=%s",
        route.name,
        params.deposit,
        params.epoch_period,
        params.min_challenge_period,
        app.outbox.sequencer_delay_limit(),
        route.devnet,
    )

    if app.challenger is None:
        app.notifier.send(
            WARNING, "no private key configured: watch-only mode (detect + alert)"
        )
    elif not app.settings.dry_run:
        if not app.challenger.can_fund_challenge():
            app.notifier.send(
                CRITICAL,
                "challenge account cannot fund a single deposit + gas reserve; "
                "the bot can only alert until funded",
            )
        app.challenger.ensure_allowance()


def tick(app: App) -> None:
    app.detector.sync_events()

    rows = app.store.active_claims()
    for row in rows:
        reconciled = app.detector.reconcile(row)
        if reconciled is None:
            continue
        row = reconciled
        if row.status in (SEEN, UNDECIDED):
            verdict = app.detector.evaluate(row)
            if verdict == Verdict.CHALLENGE:
                app.notifier.send(
                    CRITICAL,
                    f"FRAUDULENT CLAIM epoch {row.epoch}: claimed "
                    f"0x{row.claim.state_root.hex()} true {row.true_root}",
                )
                if app.challenger is not None:
                    app.challenger.challenge(row)
                else:
                    app.notifier.send(
                        CRITICAL, "watch-only mode: CANNOT challenge — intervene now"
                    )
        elif row.status == CHALLENGE_PENDING and app.challenger is not None:
            # A previous attempt failed or is stuck: retry every tick (with
            # same-nonce fee replacement) until Challenged lands or the
            # detector moves the row (front-run, verified, ...).
            app.challenger.challenge(row)

    if app.resolver is not None:
        active = [
            r
            for r in app.store.active_claims()
            if r.status in (CHALLENGED, SNAPSHOT_SENT, L1_EXECUTED, RESOLVED)
        ]
        if active and app.resolver.bridge_shutdown():
            for row in active:
                try:
                    if row.status == RESOLVED:
                        # Already ruled in our favor: withdrawChallengeDeposit
                        # has no OnlyBridgeRunning guard and pays the reward.
                        app.resolver.withdraw(row)
                    else:
                        app.resolver.escape_hatch(row)
                except Exception:  # noqa: BLE001 - one row must not block the rest
                    log.exception("shutdown handling failed for epoch %s", row.epoch)
        else:
            app.resolver.tick(active)


def run_loop(app: App) -> None:
    startup_checks(app)
    app.notifier.send(
        INFO,
        f"watcher started (route={app.route.name}, dry_run={app.settings.dry_run})",
    )
    while True:
        started = time.monotonic()
        try:
            tick(app)
        except Exception:  # noqa: BLE001 - the loop must survive anything transient
            log.exception("tick failed")
        elapsed = time.monotonic() - started
        time.sleep(max(1.0, app.settings.poll_interval - elapsed))
