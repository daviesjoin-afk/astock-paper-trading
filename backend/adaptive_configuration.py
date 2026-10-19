"""Canonical adaptive config reads and unattended-apply safety policy."""
import json

_AUTO_APPLY_KEYS = (
    "risk_auto_apply_conservative",
    "selection_auto_apply_bounded",
    "llm_realtime_auto_apply",
)
_SHADOW_POLICY = "shadow_only"


def read_config(conn):
    result = {}
    for row in conn.execute("SELECT key,value FROM adaptive_config"):
        try:
            result[row["key"]] = json.loads(row["value"])
        except (ValueError, TypeError):
            result[row["key"]] = row["value"]
    # Existing installations may have persisted the old opt-out defaults.
    # Normalize them at every read so a stale database/config cannot silently
    # reactivate an apply path.  Human apply wrappers do not use these flags.
    for key in _AUTO_APPLY_KEYS:
        result[key] = False
    result["evolution_mode"] = _SHADOW_POLICY
    return result
