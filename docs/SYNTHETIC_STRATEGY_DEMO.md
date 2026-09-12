# Synthetic StrategyPlugin demo

`backend/synthetic_demo_strategy.py` is a deliberately small, fully offline example of the strategy plugin boundary.

It uses only fixed synthetic rows and an in-memory SQLite registry. Nothing in the example reads market feeds, the paper-trading database, account state, API keys, or the process environment.

Run it from a fresh clone with the repository's normal Python environment:

```bash
python backend/synthetic_demo_strategy.py
```

The example exercises the same public plugin surface used by the platform:

1. create a temporary `synthetic_demo` strategy definition in memory;
2. register a `StrategyPlugin` with declared factor inputs;
3. run deterministic factor scoring and candidate selection;
4. read the plugin opening-event and entry-economics contracts;
5. read the compiled risk profile and exit contract;
6. unregister only the plugin instance created by this demo and close the in-memory database.

The synthetic rows are intentionally named `SYNTH_A` through `SYNTH_D`; they are not securities. The demo reuses the audited `tq_breakout` entry/exit policy family because it defines both opening-event and entry-economics contracts, allowing the example to exercise the complete entry surface. It never creates a real paper account and never places an order.

Expected candidate ordering is stable:

```text
SYNTH_A  score=0.830
SYNTH_B  score=0.765
```

The CI regression test is `backend/test_synthetic_demo_strategy.py`. It checks deterministic output, registry cleanup, collision-safe cleanup, entry/risk/exit contract coverage, fail-closed missing factors, and absence of environment/account material from the emitted example payload.
