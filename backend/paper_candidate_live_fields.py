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
            first = next(iter(values))
            table.loc[first, target] = values[first]
        table.loc[list(values), target] = pd.Series(values)


def overlay_factor_inputs(table, live_map):
    """Keep the original numeric/price gate while assigning three whole columns."""
    fields = ("price", "amount", "turnover")
    rows = {
        code: {field: row[field] for field in fields
               if isinstance(row.get(field), (int, float))}
        for code, row in live_map.items()
        if isinstance(row.get("price"), (int, float))
    }
    overlay(table, rows, {field: field for field in fields})
