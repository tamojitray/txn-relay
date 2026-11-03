from decimal import Decimal

import pytest

from app.config import Settings
from app.repository import Job
from app.schemas import TransactionWebhook
from app.worker import TransactionWorker
from tests.fakes import FakeRepository


@pytest.mark.asyncio
async def test_worker_marks_job_processed() -> None:
    repository = FakeRepository()
    payload = TransactionWebhook(
        transaction_id="txn_worker",
        source_account="source",
        destination_account="destination",
        amount=Decimal("10.00"),
        currency="INR",
    )
    await repository.create_transaction(payload)
    worker = TransactionWorker(
        repository,
        Settings(processing_delay_seconds=0, database_url="postgresql://unused/unused"),
    )

    await worker.process_job(Job(transaction_id="txn_worker", attempts=1))

    result = await repository.get_transaction("txn_worker")
    assert result is not None
    assert result.status == "PROCESSED"
    assert result.processed_at is not None
