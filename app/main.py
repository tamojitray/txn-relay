import logging
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Request, status

from app.config import get_settings
from app.database import Database
from app.repository import PostgresTransactionRepository, Repository
from app.schemas import (
    HealthResponse,
    TransactionStatus,
    TransactionWebhook,
    WebhookAcknowledgement,
)
from app.worker import TransactionWorker


def get_repository(request: Request) -> Repository:
    repository = getattr(request.app.state, "repository", None)
    if repository is None:
        raise RuntimeError("Application repository has not been initialized")
    return repository


RepositoryDependency = Annotated[Repository, Depends(get_repository)]


def create_app(repository: Repository | None = None, start_runtime: bool = True) -> FastAPI:
    settings = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if repository is not None:
            app.state.repository = repository

        if not start_runtime:
            yield
            return

        logging.basicConfig(
            level=getattr(logging, settings.log_level.upper(), logging.INFO),
            format="%(asctime)s %(levelname)s %(name)s %(message)s",
        )
        database = Database(settings.database_url)
        await database.initialize(settings.auto_create_schema)
        postgres_repository = PostgresTransactionRepository(database.session_factory)
        worker = TransactionWorker(postgres_repository, settings)
        app.state.repository = postgres_repository
        app.state.database = database
        app.state.worker = worker
        await worker.start()
        try:
            yield
        finally:
            await worker.stop()
            await database.close()

    app = FastAPI(
        title=settings.app_name,
        version="1.0.0",
        description="Durable and idempotent transaction webhook processor.",
        lifespan=lifespan,
    )
    if repository is not None:
        # Makes the application factory easy to exercise without a live database.
        app.state.repository = repository

    @app.get("/", response_model=HealthResponse, tags=["health"])
    async def healthcheck(repo: RepositoryDependency) -> HealthResponse:
        try:
            await repo.healthcheck()
        except Exception as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Database is unavailable",
            ) from exc
        return HealthResponse(status="HEALTHY", current_time=datetime.now(UTC))

    @app.post(
        "/v1/webhooks/transactions",
        response_model=WebhookAcknowledgement,
        status_code=status.HTTP_202_ACCEPTED,
        tags=["transactions"],
    )
    async def receive_transaction(
        payload: TransactionWebhook,
        repo: RepositoryDependency,
    ) -> WebhookAcknowledgement:
        created = await repo.create_transaction(payload)
        return WebhookAcknowledgement(
            transaction_id=payload.transaction_id,
            status="PROCESSING",
            duplicate=not created,
        )

    @app.get(
        "/v1/transactions/{transaction_id}",
        response_model=list[TransactionStatus],
        tags=["transactions"],
    )
    async def get_transaction_status(
        transaction_id: str,
        repo: RepositoryDependency,
    ) -> list[TransactionStatus]:
        transaction = await repo.get_transaction(transaction_id)
        if transaction is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Transaction not found",
            )
        # The assessment contract shows this endpoint returning a one-item array.
        return [TransactionStatus(**transaction.__dict__)]

    return app


app = create_app()
