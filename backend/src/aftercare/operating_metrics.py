"""Read-only metrics for the API runtime and its configured queue and ledgers."""

import json
import logging
import os
import shutil
import time
from datetime import UTC
from decimal import Decimal
from threading import Lock

import psutil
import redis
from prometheus_client.core import GaugeMetricFamily
from sqlalchemy import Numeric, cast, func, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from aftercare.conversation_analysis.tables import ChatModelAttempt
from aftercare.dispatch import DispatchJob
from aftercare.evaluation.workbench_jobs import monitoring_scope, read_start, store
from aftercare.intake_audit import IntakeModelAudit
from aftercare.intelligence.tables import ModelAttempt

log = logging.getLogger(__name__)


def metric(name, value):
    return GaugeMetricFamily("aftercare_" + name, name.replace("_", " "), value=float(value))


class BenchmarkBilling:
    """Tail complete ledger lines. Completed batches already own a frozen billing summary."""

    def __init__(self):
        self.tails = {}
        self.lock = Lock()

    def collect(self, scope):
        known, pending = Decimal(0), 0
        active = set()
        with self.lock:
            for start in store().glob("*/started"):
                if read_start(start)["scope"] != scope:
                    continue
                folder = start.parent
                final = folder / "outcome.json"
                if final.exists():
                    billing = json.loads(final.read_bytes()).get("billing")
                    if billing:
                        known += Decimal(billing["known_usd"])
                        pending += billing["unsettled"]
                        continue
                ledger = folder / "run/calls.jsonl"
                if not ledger.exists():
                    continue
                active.add(ledger)
                stamp = ledger.stat()
                inode, offset, paid, outstanding = self.tails.get(ledger, (None, 0, Decimal(0), 0))
                if inode != stamp.st_ino or stamp.st_size < offset:
                    offset, paid, outstanding = 0, Decimal(0), 0
                with ledger.open("rb") as stream:
                    stream.seek(offset)
                    for line in stream:
                        if not line.endswith(b"\n"):
                            break
                        event = json.loads(line)
                        if "reserve_usd" in event:
                            outstanding += 1
                        elif "actual_usd" in event:
                            paid += Decimal(event["actual_usd"])
                            outstanding -= 1
                        offset += len(line)
                self.tails[ledger] = stamp.st_ino, offset, paid, outstanding
                known += paid
                pending += outstanding
            self.tails = {key: self.tails[key] for key in active}
        return known, pending


class OperatingCollector:
    def __init__(self, app):
        self.app = app
        self.billing = BenchmarkBilling()

    def resources(self):
        cpu = psutil.cpu_times()._asdict()
        total = sum(v for k, v in cpu.items() if k not in ("guest", "guest_nice"))
        yield metric("cpu_seconds", total)
        yield metric("cpu_busy_seconds", total - cpu["idle"] - cpu.get("iowait", 0))
        yield metric("memory_percent", psutil.virtual_memory().percent)
        yield metric("api_rss_bytes", psutil.Process().memory_info().rss)
        yield metric("disk_free_bytes", shutil.disk_usage(os.getcwd()).free)
        net = psutil.net_io_counters()
        yield metric("network_bytes", net.bytes_recv + net.bytes_sent)

    def broker(self):
        settings = self.app.state.settings
        with redis.Redis.from_url(
            str(settings.redis_url), socket_timeout=0.75, socket_connect_timeout=0.75
        ) as client:
            queue = "aftercare-" + settings.profile
            # Kombu's Redis transport defaults to four priority lists.
            with client.pipeline(transaction=False) as pipe:
                for suffix in ("", "\x06\x163", "\x06\x166", "\x06\x169"):
                    pipe.llen(queue + suffix)
                depth = sum(pipe.execute())
            yield metric("queue_messages", depth)
            yield metric("redis_memory_bytes", client.info("memory")["used_memory"])

    def database(self):
        with Session(self.app.state.engine) as db:
            count, oldest = db.execute(
                select(func.count(), func.min(DispatchJob.created_at)).where(
                    DispatchJob.state.in_(("READY", "PUBLISHING", "PUBLISHED"))
                )
            ).one()
            yield metric("waiting_jobs", count)
            age = (
                time.time() - oldest.replace(tzinfo=oldest.tzinfo or UTC).timestamp()
                if oldest
                else 0
            )
            yield metric("oldest_wait_seconds", max(0, age))
            known, unknown = Decimal(0), 0
            for field, scale, started in (
                (ModelAttempt.estimated_cost_usd, 1, ModelAttempt.started_at),
                (ChatModelAttempt.estimated_cost_microusd, 1_000_000, ChatModelAttempt.started_at),
                (IntakeModelAudit.estimated_cost_usd, 1, IntakeModelAudit.started_at),
            ):
                total, missing = db.execute(
                    select(
                        func.sum(cast(field, Numeric(24, 10))), func.count() - func.count(field)
                    ).where(started.is_not(None))
                ).one()
                known += Decimal(total or 0) / scale
                unknown += missing
            yield metric("service_known_usd", known)
            yield metric("service_unpriced_calls", unknown)

    def benchmarks(self):
        known, pending = self.billing.collect(monitoring_scope(self.app.state.settings))
        yield metric("benchmark_known_usd", known)
        yield metric("benchmark_unpriced_calls", pending)

    def collect(self):
        for source in (self.resources, self.broker, self.database, self.benchmarks):
            try:
                yield from source()
            except (
                OSError,
                ValueError,
                KeyError,
                TypeError,
                ArithmeticError,
                psutil.Error,
                redis.RedisError,
                SQLAlchemyError,
            ) as error:
                # A failed dependency omits only its measurements; the other sources keep sampling.
                log.warning(
                    "Monitoring source %s unavailable (%s)", source.__name__, type(error).__name__
                )


class RequestTiming:
    def __init__(self, app, histogram):
        self.app, self.histogram = app, histogram

    async def __call__(self, scope, receive, send):
        path = scope.get("path", "")
        if (
            scope["type"] != "http"
            or not path.startswith("/api/")
            or path.startswith("/api/operations/")
            or path in ("/api/healthz", "/api/readyz")
        ):
            return await self.app(scope, receive, send)
        start, status = time.monotonic(), 500

        async def capture(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, capture)
        finally:
            self.histogram.labels(str(status // 100) + "xx").observe(time.monotonic() - start)
