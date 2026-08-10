# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

An independent challenger bot for the [Vea](https://vea.ninja) optimistic bridge, written in Python
from the Vea contracts and public docs only. It watches `Claimed` events on a Vea outbox,
independently re-derives every claimed state root from the inbox snapshot on the origin chain, and
challenges any claim whose root doesn't match — then drives the challenge through the canonical
bridge to resolution and withdraws the reward.

**This bot deliberately shares no code or design with the TypeScript `validator-cli` / `relayer-cli`
— do not read or reference those repos when working here.**

Full protocol facts, safety-rule derivations, and the state machine are written up in
[docs/design.md](docs/design.md) — read it before touching verdict, claims, or resolver logic; this
file only summarizes what's needed to navigate and run the code.

## Commands

```bash
uv sync                          # install deps (Python >=3.12, uv-managed)

uv run pytest                    # unit tests (no network), also runs under `uv run vea-challenger`'s deps
uv run pytest -m live            # opt-in read-only smoke tests against public testnet RPCs
uv run pytest tests/test_verdict.py                        # single file
uv run pytest tests/test_verdict.py::test_name -v           # single test

uv run vea-challenger scan       # one read-only detection pass
uv run vea-challenger run        # long-running watcher/challenger loop
uv run vea-challenger --dry-run run   # detect + alert, never spend
uv run vea-challenger status     # tracked claims + balances
uv run vea-challenger challenge --epoch N   # manual lifecycle commands,
uv run vea-challenger resolve  --epoch N    # normally driven automatically
uv run vea-challenger withdraw --epoch N    # by `run`
```

There is no separate lint/typecheck command configured in this repo (no ruff/mypy config present).

Tests are marked `live` for anything hitting real RPCs; `pytest` excludes them by default
(`addopts = "-m 'not live'"` in [pyproject.toml](pyproject.toml)).

## Architecture

```
src/vea_challenger/
  cli.py         argparse CLI: run | status | scan | challenge | resolve | withdraw
  config.py      RouteCfg / Settings (TOML + env), builtin route registry
  abi.py         hand-written minimal ABIs (inbox, outbox, WETH, ArbSys, Outbox, NodeInterface, Rollup)
  chain.py       ChainClient: multi-RPC pool, failover, quorum reads, finalized/latest, tx send+wait, EIP-1559 fees
  contracts.py   InboxContract / OutboxContract / WethToken: typed wrappers over abi.py + chain.py
  claims.py      Claim dataclass, hash_claim() (byte-exact parity w/ Solidity), hash-probing for unobservable transitions
  detector.py    scans Claimed/VerificationStarted/Challenged logs, reconstructs claim state, produces verdicts
  verdict.py     pure decision logic: HONEST / CHALLENGE / WAIT from finalized+latest snapshot reads
  challenger.py  submits challenge txs (native ETH or WETH deposit models), pre-flight funding/allowance checks
  resolver.py    sendSnapshot (L2), L2->L1 execution (L1), withdraw, bridge-shutdown escape hatch
  arbitrum.py    NodeInterface.constructOutboxProof, L2ToL1Tx event parsing, Outbox.executeTransaction
  txsender.py    nonce/fee management + tx-journal integration for both key lanes
  store.py       SQLite: claims table (state machine), tx journal, scan cursors — all writes idempotent
  watcher.py     App/build_app wiring, startup_checks(), tick()/run_loop() orchestration
  notify.py      fire-and-forget webhook notifier (Slack/Discord-compatible JSON); never blocks the loop
```

`watcher.py` is the composition root: `build_app(settings)` wires a `Store`, two `ChainClient`s
(inbox + outbox, plus an L1 client only when the route is `arb_to_gnosis`), the `Detector`, and
(if a private key is configured) `Challenger`/`Resolver`. `cli.py` calls `build_app` then dispatches
to one of `tick()`/`run_loop()` or the one-shot handlers.

### Key invariants to preserve

- **The Claim struct is never stored on-chain, only its hash** (`claimHashes[epoch]`). Every
  state-changing contract call must be given the exact current struct or it reverts. `claims.py`'s
  `hash_claim()` must stay byte-identical to the Solidity `abi.encodePacked(...)` — 85 bytes,
  `uint32` truncation, enum as one byte. Two transitions have no emitted event and require
  `probe_variants()`/`find_matching()` hash-probing: the `Verified` winner, and escape-hatch
  mutations. Don't "simplify" this into reading contract state that doesn't exist.
- **Verdicts only come from immutable inbox reads** (`verdict.py::decide`): the `finalized` L2 tag
  once its timestamp passes the epoch end, or `latest` only once it's `sequencerDelayLimit` past
  the epoch end (never earlier — a malicious sequencer can backdate `saveSnapshot` before that).
  Every configured RPC must agree (`ChainClient.read_quorum`); disagreement is a CRITICAL alert,
  never a silent fallback, unless `challenge_on_ambiguity` is explicitly set.
- **Two independent tx lanes** (`VEA_CHALLENGE_PRIVATE_KEY`, `VEA_OPS_PRIVATE_KEY`): challenge txs
  must never share a nonce sequence with slow resolution txs, or a stuck resolution can head-of-line
  block a time-sensitive challenge.
- **Tx intents are journaled in SQLite before broadcast** (`store.py` tx_journal table) so a crash
  mid-send is recoverable from the journal + on-chain scan, never double-sent.
- Deposits are always sent as **exactly** `deposit` — overpayment refunds use `.send()` with a
  2300-gas stipend, which silently burns funds sent to non-EOA addresses.

### Claim state machine (SQLite `claims.status`, defined in `store.py`)

```
SEEN → HONEST                       (root matches; terminal)
SEEN → UNDECIDED                    (waiting for L2 finality past epoch end)
SEEN/UNDECIDED → CHALLENGE_PENDING  (mismatch; tx being sent)
CHALLENGE_PENDING → CHALLENGED      (our tx or someone else's Challenged event)
CHALLENGED → SNAPSHOT_SENT          (sendSnapshot confirmed on L2)
SNAPSHOT_SENT → L1_EXECUTED         (outbox executeTransaction confirmed; ArbToGnosis: router routed)
L1_EXECUTED → RESOLVED              (Verified event, honest == Challenger)
RESOLVED → WITHDRAWN                (withdrawChallengeDeposit confirmed; terminal)
any → MISSED / LOST / DESYNC        (verified before we acted / claimer ruled honest / hash unreconstructable)
```

### Routes

Routes are data (`config.py::RouteCfg`), not code — chain ids, contract addresses, and deposit model
(`native` ETH vs `weth`/approve-transferFrom) per pair. Built-ins:
`arb-sepolia-to-sepolia-testnet` (default), `arb-sepolia-to-sepolia-devnet`,
`arb-sepolia-to-chiado-testnet`, `arb-sepolia-to-chiado-devnet`. Custom routes (e.g. mainnet) load
from a TOML file via `VEA_ROUTE_FILE` — copy the shape of a `BUILTIN_ROUTES` entry. Devnet routes
let the operator verify instantly, so devnet challenges are treated as best-effort throughout.

### Loop cadence

`run_loop` → `startup_checks` (chain-id match, genuine `finalized` support, local `hash_claim` parity
against a live contract read, funding) then `tick()` every `poll_interval` (default 30s): sync events
→ reconcile/evaluate active claims → act on verdicts → advance resolution → handle bridge shutdown.
Every action re-validates on-chain state immediately before sending.

## State files

Per-route SQLite DBs (`vea-challenger-<route>.db`) live at the repo root and are runtime state, not
source — don't edit them by hand; use `store.py`'s methods or the CLI.
