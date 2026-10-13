"""Apply current candidate fields by column without changing missing-value rules."""
import pandas as pd


def overlay(table, live_map, field_map):
    """Mutate the same table, retaining row order and untouched fallback cells."""
    rows = {code: row for code, row in live_map.items() if code in table.index}
    for target, source in field_map.items():
        values = {
            code: row[source] for code, row in rows.items()
            if row.get(source) is not None
            and str(row[source]).strip() not in {"", "--", "-"}
        }
        if not values:
            continue
        if target not in table:
            table[target] = float("nan")
        table.loc[list(values), target] = pd.Series(values)
