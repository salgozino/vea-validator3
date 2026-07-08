# Vea Challenger Bot — Design

Date: 2026-07-07
Status: v2 — revised after adversarial red-team review (22 findings; all HIGH/CRITICAL adopted)

## Mission

An independent watcher/challenger for the Vea optimistic bridge. It watches `Claimed`
events on a Vea outbox, independently verifies each claimed state root against the
Vea inbox snapshot on the origin chain, and challenges any claim whose state root
does not match. It then drives the challenge to resolution (send snapshot through the
canonical bridge, execute the L2→L1 message, withdraw the reward).

Built from the contracts and public docs only — deliberately shares no code or design
with `validator-cli` / `relayer-cli` (never read).

## Protocol facts the design relies on (from contracts)

- **Inbox (origin chain, e.g. Arbitrum)** — `VeaInboxArbToEth` / `VeaInboxArbToGnosis`:
  - `snapshots(epoch) → bytes32`: state root of the inbox merkle tree, saved by anyone
    calling `saveSnapshot()` during that epoch (L2 clock: `epoch = block.timestamp / epochPeriod`).
    May be overwritten within the epoch; immutable once the L2 clock passes the epoch end.
    `snapshots(epoch) == 0x0` if nobody saved a snapshot in that epoch.
  - `sendSnapshot(epoch, claim)`: permissionless; sends `resolveDisputedClaim(epoch, snapshots[epoch], claim)`
    through the canonical bridge (ArbSys L2→L1 message). Emits `SnapshotSent(epoch, ticketId)`.
- **Outbox (destination chain, e.g. Ethereum / Gnosis)** — `VeaOutboxArbToEth` / `VeaOutboxArbToGnosis`:
  - `claim(epoch, stateRoot)`: requires `epoch == block.timestamp/epochPeriod - 1` (L1 clock),
    nonzero root, deposit (native ETH for ArbToEth; WETH `transferFrom` for ArbToGnosis).
    Stores `claimHashes[epoch] = hashClaim(Claim{...})`, emits `Claimed(claimer, epoch, stateRoot)`.
  - `challenge(epoch, claim)` (+ ArbToEth-only overload with withdrawal address): requires the
    exact current preimage of `claimHashes[epoch]`, a deposit, `claim.challenger == 0`,
    `claim.honest == None`. Sets `challenger`, emits `Challenged(epoch, challenger)`.
    **Permissionless in all variants including devnet.**
  - `startVerification(epoch, claim)`: only after `now - timestampClaimed >= sequencerDelayLimit + epochPeriod`
    (devnet variant: no delay). Stamps `timestampVerification`/`blocknumberVerification`.
  - `verifySnapshot(epoch, claim)`: only if unchallenged and censorship test passed
    (`now - timestampVerification >= minChallengePeriod` and few missing blocks). Sets `honest = Claimer`.
  - `resolveDisputedClaim(epoch, trueRoot, claim)`: only from the canonical bridge; sets
    `honest = Challenger` if `claim.stateRoot != trueRoot` and a challenger exists; else `honest = Claimer`.
  - `withdrawChallengeDeposit(epoch, claim)`: pays challenger `depositPlusReward = 1.5 × deposit`, burns half the claimer deposit.
  - `hashClaim` = `keccak256(abi.encodePacked(stateRoot, claimer, uint32 timestampClaimed, uint32 timestampVerification, uint32 blocknumberVerification, uint8 honest, address challenger))`.
- The Claim struct is never stored, only its hash. A challenger must **reconstruct the
  struct from events + block headers** and keep it current across `startVerification` /
  `challenge` transitions, or the challenge tx reverts with "Invalid claim.".
- **Challenge window** (non-devnet): from claim until `verifySnapshot` succeeds, i.e. at least
  `epochPeriod + sequencerDelayLimit + minChallengePeriod` after `timestampClaimed`
  (testnet ArbToEth: 2h + 24h + 3h ≥ 29h). Devnet: operator can verify immediately after
  claiming, so the window is effectively unbounded only if the operator idles — treat devnet
  challenges as best-effort.
- **Resolution flow (ArbToEth)**: challenge → `sendSnapshot(epoch, claim)` on Arbitrum →
  ArbSys `L2ToL1Tx` → wait for the assertion to confirm on L1 → `Outbox.executeTransaction`
  with a proof from `NodeInterface.constructOutboxProof` → outbox `resolveDisputedClaim`
  runs → `Verified` → `withdrawChallengeDeposit`.
  - `sendSnapshot` does **not** validate the claim struct; it must be the exact
    **post-challenge** struct (challenger set, honest=None) or L1 resolution emits
    `FailedResolution` and the multi-day round trip is wasted. `sendSnapshot` is
    permissionless and repeatable → the bot watches `FailedResolution` and re-sends.
- **Resolution flow (ArbToGnosis)**: `sendSnapshot(epoch, gasLimit, claim)` (extra AMB
  gas-limit arg, clamped to `amb.maxGasPerTx()`); the L2→L1 message targets
  `RouterArbToGnosis.route(...)` on Ethereum, which forwards through the AMB to the Gnosis
  outbox (AMB oracles relay; the bot monitors delivery and alerts if it stalls).
- If the bridge shuts down (`timeoutEpochs` without progress) before resolution lands,
  `resolveDisputedClaim` reverts (`OnlyBridgeRunning`); the deposit is recovered via
  `withdrawChallengerEscapeHatch` (no reward).

## Decisions (made autonomously)

1. **Scope**: full challenger lifecycle — detect, challenge, resolve, withdraw. Oracle
   (claiming) and relaying are out of scope.
2. **Routes**: route-config-driven. Ships with built-in configs for
   `arb-sepolia→sepolia` (testnet + devnet) and `arb-sepolia→chiado` (testnet + devnet).
   Mainnet routes addable via TOML without code changes. Two deposit models:
   `native` (ETH) and `weth` (approve/transferFrom).
3. **Stack**: Python ≥3.12, `uv` project. `web3` v7 (only hard dependency),
   `pydantic`/`pydantic-settings` for config, `typer`-free — plain `argparse` (fewer deps),
   stdlib `sqlite3` for state, stdlib `logging` with JSON option. Tests: `pytest`.
4. **Independence from RPC trust**: every verdict read uses the Arbitrum `finalized` block
   tag; optional N-of-M cross-check across multiple RPC URLs per chain. The verdict value
   (`inbox.snapshots(epoch)`) is exactly the value `resolveDisputedClaim` will receive, so
   agreement between finalized reads on independent RPCs is the strongest check available
   without running a node.
5. **Verdict rule** for claim on epoch E with root C (`epochEnd = (E+1) × epochPeriod`):
   - **Primary**: wait until the **finalized** L2 block timestamp ≥ `epochEnd` (snapshot then
     immutable — finalized history cannot be backdated into). Read `T = inbox.snapshots(E)`
     at `finalized` on **all** configured L2 RPCs; they must agree. `T != C` (incl. `T == 0`)
     → **challenge**. `T == C` → HONEST (terminal; requires the full N-of-N agreement since
     it is irreversible).
   - **Fallback** (finality stalled): decide from the `latest` tag **only when** the latest
     L2 timestamp ≥ `epochEnd + outbox.sequencerDelayLimit()` — before that, a malicious
     sequencer can still backdate a `saveSnapshot` into epoch E and N-of-M RPC agreement is
     worthless (all RPCs mirror one sequencer feed). This still leaves ≥ `minChallengePeriod`
     of challenge window, because `startVerification` waits for the same delay.
   - On RPC disagreement or undecidable-by-deadline: CRITICAL alert. Auto-challenge in that
     situation is **opt-in** (`challenge_on_ambiguity`, default off): a wrong challenge loses
     the **entire** deposit (0.5·d burned + 0.5·d awarded to the claimer), risk/reward 2:1
     against, so it is a human decision by default.
   - Deadlines use `outbox.sequencerDelayLimit()` read each tick (that value gates
     `startVerification`, not the live sequencer-inbox value).
6. **Claim reconstruction**: maintain claim state machine per epoch from logs
   (`Claimed`, `VerificationStarted` [timestamp/blocknumber from the log's block header],
   `Challenged`) — the struct is never stored on-chain, only its hash. Before any tx,
   recompute `hashClaim` locally and require it to equal `claimHashes(epoch)` on-chain.
   Two contract quirks require **hash probing** (try candidate structs until one matches
   `claimHashes(epoch)`):
   - `Verified` does not say who won: probe `honest ∈ {Claimer, Challenger}`. Never assume
     victory; `withdrawChallengeDeposit` only after the Challenger-variant hash matches.
   - Escape-hatch functions (bridge shutdown) zero `claimer`/`challenger` **without events**:
     on any unexplained `claimHashes` change, probe the bounded mutation set
     ({claimer, 0} × {challenger, 0} × {honest values}) and alert.
   `hashClaim` parity: `abi.encodePacked(bytes32, address, uint32, uint32, uint32, uint8, address)`
   — 85 bytes, uint32 truncation, enum as one byte. Verified at startup against a live
   `claimHashes` read whenever a claim exists (golden-vector unit tests too).
7. **Concurrency/liquidity**: capital-bounded, not constant-bounded — a challenge locks a
   full deposit for the entire resolution round-trip (days). Challenge while
   `balance - gas_reserve ≥ deposit`; when a fraudulent claim must be skipped for lack of
   funds that is a CRITICAL page (an unchallenged fraud verifies and the bridge relays a
   false root), not an info log.
8. **Reorg safety**: event scanning lags the head by `confirmations` blocks (default 3
   Ethereum, 12 Gnosis); a challenge counts as durable only after confirmations **and**
   `claimHashes(epoch)` matching the challenged struct; cursor + dedupe in SQLite; all
   writes idempotent, safe to restart at any point. Tx intent is journaled **before**
   broadcast (nonce, params) so a crash mid-send is recoverable from the journal + mempool
   + `SnapshotSent`/`Challenged` event scans.
9. **Gas policy**: EIP-1559; the challenge tx escalates fees as the hard deadline
   (earliest possible `verifySnapshot`) approaches and is replaceable by nonce; resolution
   txs use normal fees. Always send **exactly** `deposit` (the overpayment-refund path uses
   `.send()` with a 2300 gas stipend — worthless to contracts). The bot requires its
   addresses to be EOAs for the same reason.
10. **Two tx lanes**: the deadline-critical challenge lane and the slow resolve/withdraw
    lane use separate keys (`VEA_CHALLENGE_PRIVATE_KEY`, `VEA_OPS_PRIVATE_KEY`; if only one
    is set it is used for both, with a warning) so a stuck resolution tx can never
    head-of-line-block a challenge on the shared nonce sequence.
11. **Ops**: single long-running process per route (`run` command), plus one-shot
    subcommands (`status`, `scan`, `challenge`, `resolve`, `withdraw`) for manual control;
    optional generic webhook notifications (Slack/Discord-compatible JSON) on every
    important event; `--dry-run` mode that detects and alerts but never spends. Startup
    self-checks: `finalized` tag genuinely supported (finalized < latest and advancing),
    hash parity, key balances, WETH allowance (Gnosis routes: one-time max approve;
    balance monitored separately from gas).

## Architecture

```
src/vea_challenger/
  cli.py         argparse CLI: run | status | scan | challenge | resolve | withdraw
  config.py      RouteConfig / Settings (TOML + env), builtin route registry
  abi.py         hand-written minimal ABIs (inbox, outbox, WETH, ArbSys, Outbox, NodeInterface, Rollup)
  chain.py       ChainClient: multi-RPC Web3 pool, finalized/latest reads, tx send+wait, fee logic
  claims.py      Claim dataclass, hash_claim() (parity w/ Solidity), event→state reconstruction
  detector.py    scans Claimed logs, tracks claim lifecycle, produces verdicts
  challenger.py  submits challenge txs (native + WETH deposit models), pre-flight checks
  resolver.py    sendSnapshot on L2, L2→L1 message execution on L1, withdraw
  arbitrum.py    NodeInterface.constructOutboxProof, L2ToL1Tx event parsing, Outbox.executeTransaction
  store.py       SQLite: claims table (state machine), tx journal, scan cursors
  watcher.py     orchestration loop: scan → verdict → act → resolve → withdraw
  notify.py      webhook notifier (fire-and-forget, never blocks the loop)
```

Loop cadence: one tick every `poll_interval` (default 30s): advance cursors, update claim
states, evaluate verdicts, fire due actions. Every action re-validates on-chain state first.

### Claim state machine (SQLite `claims.status`)

```
SEEN → HONEST                       (root matches; terminal)
SEEN → UNDECIDED                    (waiting for L2 finality past epoch end)
SEEN/UNDECIDED → CHALLENGE_PENDING  (mismatch; tx being sent)
CHALLENGE_PENDING → CHALLENGED      (our tx or someone else's Challenged event)
CHALLENGED → SNAPSHOT_SENT          (sendSnapshot confirmed on L2)
SNAPSHOT_SENT → L1_EXECUTED         (outbox executeTransaction confirmed; ArbToGnosis: router routed)
L1_EXECUTED → RESOLVED              (Verified event, honest == Challenger)
RESOLVED → WITHDRAWN                (withdrawChallengeDeposit confirmed; terminal)
any → MISSED / LOST                 (verified before we acted / resolution says claimer honest)
```

### Error handling

- RPC failures: rotate through the pool with backoff; the loop never dies on transient errors.
- Tx failures: journal every attempt (nonce, hash, gas); recover in-flight txs on restart by
  checking receipt before re-sending; never double-challenge (re-check `claimHashes` +
  reconstructed `challenger == 0` immediately before send).
- Clock skew: all deadlines computed from chain timestamps, never local time.

### Testing

- Unit: `hash_claim` golden vectors (recomputed keccak of `encodePacked`), claim
  reconstruction from synthetic logs, verdict logic (match/mismatch/zero-snapshot/finality-lag),
  store idempotency, deadline math.
- Integration (opt-in, `-m live`): read-only against public Sepolia/ArbSepolia RPCs —
  resolves current epoch, reads `claimHashes`, verifies ABI correctness.
- No fork-based e2e in v1 (needs funded keys and hours of wall-clock; manual runbook instead).

## Config surface (env + TOML)

- `VEA_PRIVATE_KEY` (env only), `VEA_ROUTE` (builtin name or path to TOML),
  per-chain `rpc_urls` lists, `poll_interval`, `confirmations`, `max_concurrent_challenges`,
  `safety_margin_hours`, `challenge_on_disagreement`, `dry_run`, `webhook_url`,
  `db_path`.

## Known limitations (documented, accepted for v1)

- Trusts RPC N-of-M consensus rather than running a light client/full node.
- Devnet routes: operator can insta-verify, so devnet challenges are best-effort.
- Does not perform the oracle (honest-claim) role; a fully idle bridge stays idle.
- AMB relay on ArbToGnosis assumed live (oracle-operated); bot only monitors it.
