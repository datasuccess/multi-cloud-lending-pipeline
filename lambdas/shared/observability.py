"""Powertools Logger + Metrics wrapper.

The Logger emits structured JSON; the Metrics object emits CloudWatch EMF.
Both share a service name so log lines and metric streams are joinable in
CloudWatch Logs Insights.
"""

from __future__ import annotations

import os

from aws_lambda_powertools import Logger, Metrics
from aws_lambda_powertools.metrics import MetricUnit

DEFAULT_NAMESPACE = "Lending/Generators"


def get_logger(service: str) -> Logger:
    return Logger(service=service, level=os.environ.get("LOG_LEVEL", "INFO"))


def get_metrics(service: str, namespace: str = DEFAULT_NAMESPACE) -> Metrics:
    return Metrics(namespace=namespace, service=service)


def record_run_metrics(
    metrics: Metrics,
    *,
    source: str,
    rows: int,
    bytes_: int,
    duration_ms: int,
    channel_counts: dict[str, int] | None = None,
) -> None:
    metrics.add_dimension(name="Source", value=source)
    metrics.add_metric(name="rows_written", unit=MetricUnit.Count, value=rows)
    metrics.add_metric(name="bytes_written", unit=MetricUnit.Bytes, value=bytes_)
    metrics.add_metric(name="duration_ms", unit=MetricUnit.Milliseconds, value=duration_ms)
    metrics.add_metric(name="heartbeat", unit=MetricUnit.Count, value=1)
    if channel_counts:
        for channel, count in channel_counts.items():
            metrics.add_metric(
                name=f"rows_by_channel_{channel}",
                unit=MetricUnit.Count,
                value=count,
            )
