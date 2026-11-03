from dataclasses import replace
from datetime import UTC, datetime

from app.repository import Job, TransactionRecord
from app.schemas import TransactionWebhook


class FakeRepository:
    def __init__(self) -> None:
        self.transactions: dict[str, TransactionRecord] = {}
        self.claimed: list[Job] = []
        self.failures: list[str] = []

    async def create_transaction(self, payload: TransactionWebhook) -> bool:
        if payload.transaction_id in self.transactions:
            return False
        self.transactions[payload.transaction_id] = TransactionRecord(
            transaction_id=payload.transaction_id,
            source_account=payload.source_account,
            destination_account=payload.destination_account,
            amount=payload.amount,
            currency=payload.currency,
            status="PROCESSING",
            created_at=datetime.now(UTC),
            processed_at=None,
        )
        return True

    async def get_transaction(self, transaction_id: str) -> TransactionRecord | None:
        return self.transactions.get(transaction_id)

    async def claim_jobs(self, batch_size: int, lease_seconds: int, max_attempts: int) -> list[Job]:
        jobs, self.claimed = self.claimed[:batch_size], self.claimed[batch_size:]
        return jobs

    async def mark_processed(self, transaction_id: str) -> None:
        current = self.transactions[transaction_id]
        self.transactions[transaction_id] = replace(
            current,
            status="PROCESSED",
            processed_at=datetime.now(UTC),
        )

    async def mark_failed(
        self, transaction_id: str, attempts: int, max_attempts: int, error: str, retry_seconds: int
    ) -> None:
        self.failures.append(transaction_id)

    async def healthcheck(self) -> None:
        return None
