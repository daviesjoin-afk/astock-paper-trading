## Summary

Describe what this PR changes and why the change is needed.

## Scope

- Affected strategy / module:
- User-visible behavior:
- Data or execution assumptions changed:

## Validation

Please include the commands you ran and the relevant result.

```bash
python -m unittest discover -s backend -p "test_*.py" -v
```

Add focused regression tests for changes to execution, risk, quote freshness, concurrency, strategy entry/exit logic or audit semantics.

## Safety / reproducibility checklist

- [ ] I did not add broker routing or real-money execution.
- [ ] A-share hard constraints (T+1, lot size, price limits, suspension and freshness checks) remain fail-closed unless the PR explicitly documents a safer replacement.
- [ ] No API keys, tokens, credentials, private server paths, runtime databases or real holdings are included.
- [ ] Point-in-time/replay behavior does not consume future data.
- [ ] Risk/audit behavior remains traceable with stable structured fields where applicable.
- [ ] Tests and documentation are updated for behavior changes.

## Reviewer notes

Call out anything that deserves extra scrutiny, especially changes involving order sizing, shared capital, risk exits, data-source fallbacks, timestamps or concurrent writes.
