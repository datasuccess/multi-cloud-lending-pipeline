# Anomaly injection — operating philosophy

## Why

Phase 1 ships three CloudWatch alarms (errors / freshness / low-volume) and a CloudWatch dashboard. Without traffic that *exercises* those alarms, they're just configuration that has never been tested in anger. The first time a real incident happens is exactly the wrong moment to find out the SNS subscription was never confirmed, or that the freshness alarm has the wrong threshold.

The anomaly injection engine is how we keep the monitoring stack honest: in **`MODE=test`**, every Lambda invocation rolls a single die and may inject one of four anomalies. Over a few days of hourly runs we see every alarm transition through ALARM and back to OK without a human touching the Lambda code or the data.

In **`MODE=prod`** the engine returns `Anomaly.NONE` unconditionally. Production runs are deterministic — no dice rolled, no sleeps, no sabotage.

## What the four anomalies do

All four are mutually exclusive — one die roll picks at most one per run.

| Anomaly | Default prob | What it does | Alarm exercised |
|---|---:|---|---|
| `SKIP` | 3% | Returns immediately; no parquet, manifest, ledger entry, or metrics. Lambda exits cleanly so the errors metric stays at 0. | **Freshness (P1)** — fires after the missing-heartbeat window expires. |
| `UNDERSHOOT` | 10% | Rewrites `rows_n` to a uniform integer in `[100, 450]`. Validation passes (test mode runs with `MIN_ROWS=1`) and `_SUCCESS` is written, but the `rows_written` EMF metric is well under the test-mode threshold (400). | **Low-volume (P2)** — most undershoots breach, a few squeak through (the threshold sits inside the range deliberately). |
| `SILENT_FAIL` | 5% | Parquet + manifest land normally, then we append a chaos error after `validate_table` to force the no-`_SUCCESS` path. Lambda raises → `AWS/Lambda Errors` increments. | **Errors (P1)** — the most aggressive, pages on a 5-minute window. |
| `SLOW` | 5% | Sleeps `SLOW_SLEEP_SECONDS=25` after the parquet write, then continues normally. Pushes `duration_ms` above its baseline. | **Duration widget on the dashboard** (no alarm yet — this is the seed for a future P3 alarm). |

The probabilities are tuned to give roughly 3–5 anomalous events per day under hourly invocation — enough to see real alarm transitions, low enough that the dashboard remains predominantly green.

Override any individual probability via env var: `ANOMALY_SKIP_PROB`, `ANOMALY_UNDERSHOOT_PROB`, `ANOMALY_SILENT_FAIL_PROB`, `ANOMALY_SLOW_PROB`. Set all four to `0` and you have test cadence (hourly) with no chaos.

## How tests use it

`run()` accepts an `anomaly: Anomaly | None` parameter so the e2e tests can assert each path deterministically without monkeypatching `random`. The `pick_anomaly()` helper itself takes both an `AnomalyConfig` and a `random.Random` so unit tests stay deterministic too.

## Where this differs from chaos engineering proper

Chaos engineering injects faults at the *infrastructure* level (kill a pod, drop network, throttle a disk). This injects faults at the *application* level: it doesn't tell us what happens when AWS goes down, but it does tell us what happens when our own code emits a bad signal. That's the failure mode we control and the one that has to be observable before the harder ones are worth investigating.
