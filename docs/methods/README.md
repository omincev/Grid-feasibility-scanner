# Method cards

One card per calculation, so anyone can tell a field expert exactly how a served number was computed. A card is `docs/methods/<method_id>.md`: YAML front matter, then the formula in plain words.

```yaml
---
id: congestion_score              # = file name; output rows record it with the version
version: 1                        # raise it when the calculation changes (H7 sign-off)
title: Congestion score (zones)
status: provisional               # provisional | approved
owner: [oleg, michael]
inputs: [f1_intraday_spread_hourly, ...]   # codebook equivalence classes or upstream outputs
parameters: [score_weights.weights, ...]   # <role>.<parameter>, resolved through the scenario's sets
outputs: [feat.score (level zone)]
known_biases: [...]
---
```

Code declares each method it implements once, with `scanner.core.methods.MethodRef.declare(id, version, parameters)`, and records the ID and version on every row it writes (`method_id`, `method_version`, `method_versions` columns; `val.run.method`). `make contracts` fails when code references a method without a card, when the card's version or parameters differ from the code's, and when a card parameter does not resolve in every scenario (`assumptions/scenarios/`). `load-curated` loads the cards into `ref.method_card`; `serving.method_catalog` and `exports/methods_and_scenarios.html` show them.

Parameters are named by role, not by set: which set (and which values) a role means is the scenario's choice. Features take no parameters: they are computed once for every scenario.
