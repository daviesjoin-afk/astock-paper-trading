R27-B2C-1 made **execution** a real fact owner: it publishes its own verification vocabulary, identity and business day. But the research contract could not yet *hear* it. `ResearchEvidenceRef` carried `verification` / `verification_method` — a pair of **market-shaped** fields — and `_owner_verification_pair()` only ever asked whether a `MarketDataSnapshot` was legal. So the research contract still spoke market verification vocabulary, and every other owner had exactly two ways in:

```
1. pretend to be market_data;
2. translate its own verdict into market words.
```

Translating is inventing a verification conclusion, which is the one thing R27-A exists to prevent.

This PR removes that structural coupling: research core can now carry **owner-native** verification.

Base: `master` @ `6709d9c0dc15138437a035506aeb883d45bd4e1c` (#194 merged).

## Scope

One capability, in one module:

```
ResearchEvidenceRef / HypothesisEvidence:
    "is this fact owner-verified?"  no longer depends on
        verification == market_data_contract.VERIFICATION_VERIFIED
    and instead depends on the verdict the owner itself published.
```

Deliberately **not** in this PR: the execution adapter (B2C-3), any runtime migration, `execution_verification` / `execution_evidence` changes, frontend, B3.

## Before / after

```text
before:
    ResearchEvidenceRef
        verification          ← market vocabulary
        verification_method   ← market vocabulary
            ↓
        _owner_verification_pair()  — named "owner", but only asks MarketDataSnapshot
            ↓
        HypothesisEvidence.is_verified == (verification == MDC.VERIFICATION_VERIFIED)

after:
    owner factory (market today; execution at B2C-3)
        ↓
    OwnerVerification            outcome / status / attributes      (owner-neutral, immutable)
        ↓
    ResearchEvidenceRef          canonical: owner_verification
        ↓
    HypothesisEvidence.is_verified  ==  ref.is_verified
```

## Owner-neutral verification semantics

`OwnerVerification` is a stable domain concept, not a wrapper — the three fields have three different owners of meaning:

| field | meaning | who interprets it |
| --- | --- | --- |
| `outcome` | three-state, owner-neutral: `verified` / `unverified` / `source_unusable` | **research core** (the only field it consumes) |
| `status` | the owner's own status word, kept verbatim (`single_source`, execution's four states, …) | the owner + display/audit only — research **never compares it** |
| `attributes` | owner-specific immutable dimensions (`verification_method` / `cross_source_verified` for market) | **only the owner's factory** |

Three states rather than a boolean because "the verification source was unusable" and "the fact did not pass verification" must produce **different** research reasons (`evidence_unavailable` vs `evidence_not_verified`). Collapsing them into a bool makes that distinction unexpressible in the hypothesis layer.

`_derive_status` now reads `owner_verification.source_unusable` instead of `verification in (MDC.VERIFICATION_UNAVAILABLE, MDC.VERIFICATION_DISAGREEMENT)` — the last market vocabulary reference in generic hypothesis logic is gone (RVERIFY-02 asserts this statically, by AST symbol, so docstrings explaining the rule do not trip it).

## Market compatibility (unchanged, byte for byte)

B2C-2 is not the B3 cleanup, so no existing market read changes:

```
ref.verification            → R24's status word        (identical to before)
ref.verification_method     → R24's method             (identical to before)
ref.cross_source_verified   → R24's judgement          (identical to before)
```

They are now **derived compatibility properties**, not canonical storage. The projection is additive only: `is_verified` and `verification_attributes` were added; no existing key or value changed.

For **non-market** facts:

```
verification_method      = None    — explicit "not applicable", NOT MDC.VERIFICATION_METHOD_NONE
cross_source_verified    = False   — "this is not a market cross-source claim",
                                     NOT "that execution fact failed verification"
```

`MDC.VERIFICATION_METHOD_NONE` is itself market vocabulary; using it to mean "non-market owner" would put every other owner back in market's coordinate system, and would force execution/news to supply a market field they do not have.

## What is still R24's

Nothing about market verification moved:

* `(verification, verification_method)` legality → still asked of R24 by constructing a `MarketDataSnapshot`;
* `cross_source_verified` → still delegated to `MDC.is_cross_source_verified` (the contract contains **no** comparison against a `"verified"` literal — asserted by AST, not substring);
* the market-specific validator is **honestly renamed**: `_owner_verification_pair` → `_market_verification_pair`, with the owner-factory mapping in `_market_owner_verification`. Keeping a market-only validator named "owner verification" would recreate the fake generic abstraction this PR removes.

`market_data_contract.py` is untouched.

## Market outcome mapping is explicit and exhaustive (fail closed)

`_MARKET_OUTCOME_BY_VERIFICATION` maps **every** R24 state to an owner-neutral outcome explicitly:

```
verified        → verified
single_source   → unverified
not_attempted   → unverified
disagreement    → source_unusable
unavailable     → source_unusable
```

and `_market_owner_verification()` checks **both directions** against `MDC.VERIFICATIONS` before classifying.

This closed a real fail-open in an earlier revision of this branch. That version used a catch-all:

```python
if   verification == MDC.VERIFICATION_VERIFIED:            outcome = VERIFIED
elif verification in (UNAVAILABLE, DISAGREEMENT):          outcome = SOURCE_UNUSABLE
else:                                                      outcome = UNVERIFIED   # ← fail-open
```

Today's five R24 states happen to classify correctly, so the behaviour was invisible. But `_market_verification_pair()` accepts whatever R24 accepts, so a **future** valid R24 state would have silently fallen into `else` — research deciding the meaning of a state the owner never published a mapping for. That is the same class of coupling this PR exists to remove, just relocated.

With the explicit table, "R24 adds a state" stops being a silent semantic invention and becomes a contract change that must be resolved by hand:

```
R24 adds a state  →  fail closed (ValueError naming the unmapped state)  →  human decides its outcome
```

`RVERIFY-11` locks it: exhaustive coverage, both drift directions RED, all three outcomes actually used (non-vacuity), and a behavioural case that **patches `MDC.VERIFICATIONS` to simulate R24 adding a state** and requires the mapping to refuse it. The catch-all shape is additionally rejected by an AST check (so docstrings explaining the rule do not trip it).

## Owner-neutral conflict detection

`fact_state` now takes `OwnerVerification.canonical()` (outcome + status + attributes, deterministic and insertion-order independent) plus the content fingerprint.

This preserves the R24 permanent invariant — `verified + cross_source` vs `verified + coverage_integrity` are different verification conclusions and **still conflict** (RVERIFY-06) — while making "same identity + changed owner verification state → `EvidenceConflict`" true for any owner (RVERIFY-05). Comparing only `status` would have flattened the two market cases into one fact.

freshness still does **not** enter fact conflict (R27-A invariant, `AI_TYPED_08` still green).

## Issuance boundary is unchanged

`_issue_evidence_ref` now **requires** a real `OwnerVerification` (a duck-typed object is rejected with `TypeError`), so omitting it cannot issue a fact with no verdict. `ResearchEvidenceRef` still has no public constructor, and the public evidence factory set is still exactly `{market_data}`:

```
Public evidence factories:      before = market_data only   after = market_data only
SUPPORTED_OWNER_ADAPTERS:       before = {market_data}      after = {market_data}
Execution adapter:              before = 0                  after = 0
```

`execution` is **not** registered — that is B2C-3. #193's guard suite passes unchanged.

## Architecture impact

```
Roadmap capability removed:                        0
Roadmap invariant weakened:                        0
Production modules added:                          0
Production modules removed:                        0
New service/facade/manager:                        0
New abstraction layers:                            0
New domain value objects:                          1     (OwnerVerification)
Research verification model:                       before = market-shaped
                                                   after  = owner-neutral
Market verification authority:                     before = R24   after = R24
Execution verification authority:                  before = execution_verification
                                                   after  = execution_verification
Research-owned verification semantics added:       0
Market-specific verification checks in generic
    hypothesis logic:                              before = >0    after  = 0
Public evidence factories:                         before = market_data only
                                                   after  = market_data only
Execution adapter:                                 before = 0     after  = 0
Runtime migrations:                                before = current state
                                                   after  = unchanged
Compatibility fields removed:                      0
Frontend changes:                                  0
Net architecture surface:                          NEUTRAL
```

**Why the one new value object is not a wrapper.** `OwnerVerification` is the domain concept the roadmap requires: "a fact owner publishes its own verification verdict in a shape consumers can carry without interpreting it". It holds no logic that duplicates an owner rule, forwards nothing, and is not a registry or adapter base. Its three fields are consumed by three different parties (`outcome` by research core, `status` by display/audit, `attributes` by the owning factory), which is exactly why a single market-shaped pair could not represent it.

There is **no** registry, no `BaseOwnerVerificationAdapter`, no `OWNER_VALIDATORS` map, no `register_owner()`. One owner is wired into `ResearchEvidenceRef` today; the execution adapter arrives at B2C-3.

## Maintainability impact

```
New facade / wrapper:                          0
New helper / utils / manager / facade:         0
Compatibility shim:                            0
Roadmap capability removed:                    0
Roadmap invariant weakened:                    0
```

One fact verification truth remains, end to end:

```
owner factory → owner-neutral immutable verification → ResearchEvidenceRef
              → HypothesisEvidence.is_verified
```

Owner-specific vocabulary stops at the owner / owner factory and does **not** spread into hypothesis logic, service, provider, runtime or UI.

## Target — verification layer

```text
first:  python -m unittest test_ai_research_contract test_ai_research_evidence_ownership_guard
        → 66 tests OK

second: python -m unittest test_ai_research_contract test_ai_provider_transport \
            test_ai_research_service test_ai_research_evidence_ownership_guard \
            test_ai_research_repository test_execution_fact_contract
        → 213 tests OK
```

`backend/test_ai_research_contract.py` gained the `RVERIFY-*` group (11 tests):

- **RVERIFY-01** market ref compatibility surface is unchanged (all three legacy reads + projection key/values)
- **RVERIFY-02** research verification does not depend on market status vocabulary — including a synthetic `source_type=execution`, `status="owner_verified"` fact whose status word is **deliberately not** `MDC.VERIFICATION_VERIFIED`
- **RVERIFY-03** `outcome=unverified` cannot support a hypothesis regardless of status spelling, with a non-vacuity control
- **RVERIFY-04** owner verification attributes are deeply immutable (nested containers, caller-held original dict)
- **RVERIFY-05** same identity + changed owner verification state → `EvidenceConflict`, order-independent
- **RVERIFY-06** market `verification_method` remains part of conflict state; canonical form is insertion-order independent
- **RVERIFY-07** market `cross_source_verified` stays delegated to R24 (`verified + coverage_integrity` → `is_verified=True`, `cross_source_verified=False`)
- **RVERIFY-08** non-market verification does not require a market method (None, not `"none"`)
- **RVERIFY-09** `ResearchEvidenceRef` still has no public constructor; the issuer requires a real `OwnerVerification`
- **RVERIFY-10** private issuer / factory registry boundary unchanged (exactly one issuer call site, factory set not expanded)
- **RVERIFY-11** market outcome mapping exhaustively covers `MDC.VERIFICATIONS`; missing/extra state → RED; simulated "R24 adds a state" → fail closed; no catch-all fallback

`AI_TYPED_02` was updated to exercise the market validator at its new honest name — same assertion (illegal `verified` + `none` pair fails closed), now pointed at the market-specific factory.

## Mutation

`work/r27b2c2_owner_native_verification_mutation_check.py`:

```
9/9 CAUGHT, survived=0, fake=0, restore sha256 PASS
```

- M-RVERIFY-1 — `HypothesisEvidence.is_verified` goes back to `verification == MDC.VERIFICATION_VERIFIED`
- M-RVERIFY-2 — owner `is_verified=False` is treated as True
- M-RVERIFY-3 — `fact_state` ignores owner verification attributes
- M-RVERIFY-4 — `OwnerVerification.canonical` drops owner-specific attributes
- M-RVERIFY-5 — owner verification attributes stop being deep-frozen
- M-RVERIFY-6 — non-market ref is forced to carry `MDC.VERIFICATION_METHOD_NONE`
- M-RVERIFY-7 — market `cross_source_verified` stops delegating to R24
- M-RVERIFY-8 — the explicit exhaustive mapping reverts to a catch-all `else`
- M-RVERIFY-9 — the vocabulary drift check is removed (new R24 state passes silently)

Each mutation is anchored exactly once; `--non-vacuity` runs the baseline first; `SyntaxError` / `ImportError` / `NameError` count as FAKE, not caught.

## Verification

- targeted suites `67/67` and `214/214` PASS
- full backend suite `4394 tests OK (skipped=5)`
- key suites re-run on Python 3.11 and 3.12: `167/167` PASS each
- `ruff check backend` clean (ruff 0.16.6, the pinned CI version), `compileall` clean
- leak scan (worktree) `0 findings`

## Known provenance limitation

**Unchanged and not claimed closed.** Owner-neutral verification makes it possible to *carry* any owner's verdict; it does **not** prove that the input object really came from that owner. `MarketDataReading` / `ExecutionEvidence` remain publicly constructible, so the two-step forgery path still exists (`AI_TYPED_06`, and the same note on the execution side).

```
contract-issued evidence boundary = CLOSED
owner-origin provenance           = OPEN / REQUIRED
```

Generic-izing the model must not be read as closing provenance. It stays an open R27 requirement, to be closed by the owner/provenance architecture (execution → news → adaptive/experiment → runtime/incident).

## What remains intentionally deferred

```
B2C-3  execution → ResearchEvidenceRef adapter     NOT STARTED
B2C-4  migrate the first execution-backed research runtime (pnl_attribution)
B2C-5  news owner readiness
B2C-6  adaptive / experiment owner readiness
B2C-7  runtime / incident owner readiness
B2C-8  remaining deepseek_research typed convergence
B2C-9  ai_analysis lifecycle convergence
B2C-10 canonical research API/UI + delete the B2B compatibility projection
```

Also unchanged: rejected/cancelled execution facts still have no owner-recorded business day, so `business_day` remains `unknown` for them until the owner records one.

**Recorded for B2C-3 (not a blocker this round).** `_issue_evidence_ref()` currently has exactly one production caller — `evidence_ref_from_market_reading()` — which is correct, and #193's guard proves zero calls outside the contract. What is not yet locked is the *inside* of the contract: that the issuer may only be called by an approved factory (the analogue of #194's EXFACT-19). There is no actual over-reach today, so it is not treated as a blocker. When B2C-3 adds the second approved factory, the issuer caller set should become an exact allowlist:

```
B2C-2:  {evidence_ref_from_market_reading}
B2C-3:  {evidence_ref_from_market_reading, evidence_ref_from_execution_projection}
```

Caller-set equality only — no CFG analysis needed.

Docs synced in the same commit: `ARCHITECTURE.md` (R27-A verification storage, conflict state, hypothesis reason table, new B2C-2 section, B2C-1 section's forward reference) and `docs/R27_B2C_EVIDENCE_OWNER_MATRIX.md` (prerequisite list now records B2C-1/B2C-2 as done while B2C-3 stays absent, maintainability metrics, stale line references corrected).

---

MERGE:   NOT MERGED
DEPLOY:  NOT DEPLOYED
STATUS:  AWAITING HUMAN REVIEW
