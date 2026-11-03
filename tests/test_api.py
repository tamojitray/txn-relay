from decimal import Decimal

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import create_app
from tests.fakes import FakeRepository

PAYLOAD = {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": 1500,
    "currency": "inr",
}


@pytest.fixture
def repository() -> FakeRepository:
    return FakeRepository()


@pytest.fixture
async def client(repository: FakeRepository):
    app = create_app(repository=repository, start_runtime=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        yield client


@pytest.mark.asyncio
async def test_healthcheck(client: AsyncClient) -> None:
    response = await client.get("/")
    assert response.status_code == 200
    assert response.json()["status"] == "HEALTHY"
    assert response.json()["current_time"].endswith("Z")


@pytest.mark.asyncio
async def test_webhook_is_accepted_and_can_be_queried(client: AsyncClient) -> None:
    accepted = await client.post("/v1/webhooks/transactions", json=PAYLOAD)
    assert accepted.status_code == 202
    assert accepted.json() == {
        "transaction_id": "txn_abc123def456",
        "status": "PROCESSING",
        "duplicate": False,
    }

    status_response = await client.get("/v1/transactions/txn_abc123def456")
    assert status_response.status_code == 200
    results = status_response.json()
    assert len(results) == 1
    body = results[0]
    assert body["amount"] == "1500"
    assert body["currency"] == "INR"
    assert body["status"] == "PROCESSING"


@pytest.mark.asyncio
async def test_duplicate_webhook_is_idempotent(
    client: AsyncClient, repository: FakeRepository
) -> None:
    first = await client.post("/v1/webhooks/transactions", json=PAYLOAD)
    duplicate = await client.post("/v1/webhooks/transactions", json=PAYLOAD)

    assert first.status_code == duplicate.status_code == 202
    assert duplicate.json()["duplicate"] is True
    assert len(repository.transactions) == 1


@pytest.mark.asyncio
async def test_unknown_transaction_returns_404(client: AsyncClient) -> None:
    response = await client.get("/v1/transactions/missing")
    assert response.status_code == 404


@pytest.mark.asyncio
async def test_invalid_webhook_returns_422(client: AsyncClient) -> None:
    invalid = {**PAYLOAD, "amount": -1, "currency": "rupees"}
    response = await client.post("/v1/webhooks/transactions", json=invalid)
    assert response.status_code == 422
    assert "detail" in response.json()


@pytest.mark.asyncio
async def test_amount_is_preserved(repository: FakeRepository, client: AsyncClient) -> None:
    await client.post("/v1/webhooks/transactions", json={**PAYLOAD, "amount": "15.50"})
    stored = repository.transactions[PAYLOAD["transaction_id"]]
    assert stored.amount == Decimal("15.50")
