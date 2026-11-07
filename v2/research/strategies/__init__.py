"""Fixed offline research strategies.

Each module is a complete, reviewed strategy definition: a stable
`strategy_version_id`, explicit `PARAMETERS`, and a pure `signals(entries)`
function. Rules use integer arithmetic only, read no clock, use no randomness
and never decide order size, price, fees or cash.

Changing a rule, a parameter or a comment changes `source_digest`, which is part
of the strategy artifact identity: a changed strategy must ship a new reviewed
definition and a new `strategy_version_id`.
"""
