from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol

from sqlalchemy import Engine
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session

from .models import SimulatedPayment


@dataclass(frozen=True)
class PaymentResult:
    status: Literal["SUCCEEDED", "FAILED", "UNKNOWN"]
    receipt: dict | None = None


class PaymentGateway(Protocol):
    def submit(self, business_key: str, payload_hash: str, amount_cents: int) -> PaymentResult: ...
    def lookup(self, business_key: str, payload_hash: str, amount_cents: int) -> PaymentResult: ...


class LocalPaymentGateway:
    """Simulated provider state commits independently from the business ledger."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine

    def submit(self, business_key: str, payload_hash: str, amount_cents: int) -> PaymentResult:
        insert = pg_insert if self.engine.dialect.name == "postgresql" else sqlite_insert
        with Session(self.engine) as db, db.begin():
            receipt = {
                "id": f"sim-pay-{business_key}",
                "amount_cents": amount_cents,
                "mode": "SIMULATED",
                "result": "REFUND_RECORDED",
            }
            db.execute(
                insert(SimulatedPayment)
                .on_conflict_do_nothing()
                .values(
                    business_key=business_key,
                    payload_hash=payload_hash,
                    amount_cents=amount_cents,
                    receipt=receipt,
                )
            )
            return self._lookup(db, business_key, payload_hash, amount_cents)

    def lookup(self, business_key: str, payload_hash: str, amount_cents: int) -> PaymentResult:
        with Session(self.engine) as db:
            return self._lookup(db, business_key, payload_hash, amount_cents)

    @staticmethod
    def _lookup(
        db: Session, business_key: str, payload_hash: str, amount_cents: int
    ) -> PaymentResult:
        row = db.get(SimulatedPayment, business_key)
        if row is None or row.payload_hash != payload_hash or row.amount_cents != amount_cents:
            return PaymentResult("UNKNOWN")
        return PaymentResult("SUCCEEDED", row.receipt)
