from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.models import Transaction
from app.schemas import TransactionWebhook


@dataclass(frozen=True)
class Job:
    transaction_id: str
    attempts: int


@dataclass(frozen=True)
class TransactionRecord:
    transaction_id: str
    source_account: str
    destination_account: str
    amount: Decimal
    currency: str
    status: str
    created_at: datetime
    processed_at: datetime | None


class Repository(Protocol):
    async def create_transaction(self, payload: TransactionWebhook) -> bool: ...

    async def get_transaction(self, transaction_id: str) -> TransactionRecord | None: ...

    async def claim_jobs(
        self, batch_size: int, lease_seconds: int, max_attempts: int
    ) -> list[Job]: ...

    async def mark_processed(self, transaction_id: str) -> None: ...

    async def mark_failed(
        self, transaction_id: str, attempts: int, max_attempts: int, error: str, retry_seconds: int
    ) -> None: ...

    async def healthcheck(self) -> None: ...


class PostgresTransactionRepository:
    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def create_transaction(self, payload: TransactionWebhook) -> bool:
        statement = (
            insert(Transaction)
            .values(
                transaction_id=payload.transaction_id,
                source_account=payload.source_account,
                destination_account=payload.destination_account,
                amount=payload.amount,
                currency=payload.currency,
                status="PENDING",
            )
            .on_conflict_do_nothing(index_elements=[Transaction.transaction_id])
            .returning(Transaction.transaction_id)
        )
        async with self._session_factory() as session:
            result = await session.execute(statement)
            created_id = result.scalar_one_or_none()
            await session.commit()
        return created_id is not None

    async def get_transaction(self, transaction_id: str) -> TransactionRecord | None:
        async with self._session_factory() as session:
            result = await session.execute(
                select(Transaction).where(Transaction.transaction_id == transaction_id)
            )
            transaction = result.scalar_one_or_none()

        if transaction is None:
            return None
        public_status = (
            "PROCESSING" if transaction.status in {"PENDING", "PROCESSING"} else transaction.status
        )
        return TransactionRecord(
            transaction_id=transaction.transaction_id,
            source_account=transaction.source_account,
            destination_account=transaction.destination_account,
            amount=transaction.amount,
            currency=transaction.currency,
            status=public_status,
            created_at=transaction.created_at,
            processed_at=transaction.processed_at,
        )

    async def claim_jobs(self, batch_size: int, lease_seconds: int, max_attempts: int) -> list[Job]:
        # FOR UPDATE SKIP LOCKED lets multiple application replicas safely share the queue.
        statement = text(
            """
            WITH candidates AS (
                SELECT transaction_id
                FROM transactions
                WHERE attempts < :max_attempts
                  AND (
                    (status = 'PENDING' AND next_attempt_at <= NOW())
                    OR
                    (status = 'PROCESSING'
                     AND locked_at < NOW() - make_interval(secs => :lease_seconds))
                  )
                ORDER BY created_at
                LIMIT :batch_size
                FOR UPDATE SKIP LOCKED
            )
            UPDATE transactions AS transaction
            SET status = 'PROCESSING',
                locked_at = NOW(),
                attempts = transaction.attempts + 1,
                updated_at = NOW()
            FROM candidates
            WHERE transaction.transaction_id = candidates.transaction_id
            RETURNING transaction.transaction_id, transaction.attempts
            """
        )
        async with self._session_factory() as session:
            result = await session.execute(
                statement,
                {
                    "batch_size": batch_size,
                    "lease_seconds": lease_seconds,
                    "max_attempts": max_attempts,
                },
            )
            jobs = [Job(transaction_id=row.transaction_id, attempts=row.attempts) for row in result]
            await session.commit()
        return jobs

    async def mark_processed(self, transaction_id: str) -> None:
        statement = text(
            """
            UPDATE transactions
            SET status = 'PROCESSED',
                processed_at = NOW(),
                locked_at = NULL,
                last_error = NULL,
                updated_at = NOW()
            WHERE transaction_id = :transaction_id AND status = 'PROCESSING'
            """
        )
        async with self._session_factory() as session:
            await session.execute(statement, {"transaction_id": transaction_id})
            await session.commit()

    async def mark_failed(
        self,
        transaction_id: str,
        attempts: int,
        max_attempts: int,
        error: str,
        retry_seconds: int,
    ) -> None:
        final_status = "FAILED" if attempts >= max_attempts else "PENDING"
        statement = text(
            """
            UPDATE transactions
            SET status = :status,
                next_attempt_at = NOW() + make_interval(secs => :retry_seconds),
                locked_at = NULL,
                last_error = :error,
                updated_at = NOW()
            WHERE transaction_id = :transaction_id AND status = 'PROCESSING'
            """
        )
        async with self._session_factory() as session:
            await session.execute(
                statement,
                {
                    "transaction_id": transaction_id,
                    "status": final_status,
                    "retry_seconds": retry_seconds,
                    "error": error[:2000],
                },
            )
            await session.commit()

    async def healthcheck(self) -> None:
        async with self._session_factory() as session:
            await session.execute(text("SELECT 1"))
