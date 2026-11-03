# TxnRelay - Reliable Transaction Webhook Processor

I built this service to accept transaction webhooks from external payment providers, acknowledge
them quickly, and process them reliably in the background. The API is implemented with FastAPI,
while Supabase PostgreSQL is used as both the persistent transaction store and the durable job
queue.

The main engineering goals were:

- Return `202 Accepted` without waiting for slow downstream processing.
- Prevent duplicate webhook deliveries from creating duplicate transactions.
- Preserve accepted work across application restarts.
- Support concurrent workers safely.
- Make failures visible and retryable.
- Package the service as a reproducible Docker image.

## API endpoints

| Method | Endpoint | Description |
| --- | --- | --- |
| `GET` | `/` | Application and database health check |
| `POST` | `/v1/webhooks/transactions` | Accept a transaction webhook |
| `GET` | `/v1/transactions/{transaction_id}` | Retrieve the persisted transaction status |
| `GET` | `/docs` | Interactive Swagger documentation |
| `GET` | `/redoc` | ReDoc API documentation |

## Architecture

```text
Payment provider
       |
       | POST webhook
       v
FastAPI application
       |
       | Persist before acknowledging
       v
Supabase PostgreSQL
       |
       | Claim pending work atomically
       v
Async transaction worker
       |
       | Simulated 30-second external operation
       v
Supabase PostgreSQL (PROCESSED)
```

When a webhook arrives, I first persist it with a `PENDING` status. Only after the database commit
succeeds does the endpoint return `202 Accepted`. This keeps the request path short while ensuring
that acknowledged work is not held only in memory.

A worker running alongside the API polls PostgreSQL, claims eligible rows, marks them as
`PROCESSING`, performs the background operation, and finally stores `PROCESSED` with a completion
timestamp.

## Key design decisions

### Durable processing instead of in-memory background tasks

I did not use FastAPI `BackgroundTasks` for transaction processing. An in-memory task can be lost
if the process stops after returning `202`. By storing work in PostgreSQL first, an interrupted job
can be recovered and processed by another worker.

### Database-enforced idempotency

`transaction_id` is the primary key of the `transactions` table. Inserts use PostgreSQL
`ON CONFLICT DO NOTHING`, so concurrent requests containing the same transaction ID cannot create
multiple rows.

The first request returns:

```json
{
  "transaction_id": "txn_abc123def456",
  "status": "PROCESSING",
  "duplicate": false
}
```

Later requests with the same ID return `duplicate: true` without modifying the original row or
creating another job.

### Safe concurrent workers

Workers claim jobs with `FOR UPDATE SKIP LOCKED`. This allows multiple worker instances to share
the same queue without selecting the same transaction simultaneously.

### Lease-based crash recovery

When a worker claims a transaction, it records `locked_at`. If that worker stops before completing
the job, the row becomes eligible for processing again after `PROCESSING_LEASE_SECONDS`. The
default lease is 120 seconds, which is intentionally longer than the simulated 30-second operation.

### Retry handling

Processing failures are recorded in `last_error` and retried with exponential backoff. After
`MAX_PROCESSING_ATTEMPTS`, the transaction is marked `FAILED` so it remains visible for diagnosis.

## Technology choices

- **Python 3.12** - application runtime
- **FastAPI** - API framework, validation, and OpenAPI documentation
- **Pydantic** - request and response validation
- **SQLAlchemy async** - database access layer
- **asyncpg** - asynchronous PostgreSQL driver
- **Supabase PostgreSQL** - persistence and durable job queue
- **Pytest and HTTPX** - automated API and worker tests
- **Ruff** - linting and formatting
- **Docker** - reproducible application packaging

## Project structure

```text
app/
  config.py       Environment-based application settings
  database.py     Async PostgreSQL engine and connection lifecycle
  main.py         FastAPI application, lifespan, and endpoints
  models.py       SQLAlchemy transaction table definition
  repository.py   Atomic database and queue operations
  schemas.py      Pydantic request and response models
  worker.py       Background polling, processing, retries, and recovery
tests/
  fakes.py        In-memory repository used by tests
  test_api.py     Endpoint, validation, and idempotency tests
  test_worker.py  Background-worker test
Dockerfile        Production container definition
docker-compose.yml
requirements.txt
requirements-dev.txt
```

## Supabase configuration

The service connects directly to Supabase PostgreSQL. It does not require the Supabase REST URL,
anon key, or service-role key.

In Supabase, open **Project Settings -> Database -> Connection string -> Session pooler -> URI**.
Copy `.env.example` to `.env` and replace the placeholder with that URI:

```dotenv
DATABASE_URL=postgresql://postgres.PROJECT_REF:PASSWORD@aws-0-REGION.pooler.supabase.com:5432/postgres?sslmode=require
```

The password must be URL-encoded if it contains reserved URL characters. The `.env` file is
excluded from Git and the Docker build context.

With `AUTO_CREATE_SCHEMA=true`, the service creates the table and indexes on first startup. The
configured database user therefore needs permission to create and update objects in the target
schema.

## Running locally

Requirements:

- Python 3.12 or newer
- A Supabase PostgreSQL connection string

PowerShell setup:

```powershell
Copy-Item .env.example .env
# Add the real DATABASE_URL to .env

python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements-dev.txt
uvicorn app.main:app --reload
```

The service is then available at <http://localhost:8000>, with Swagger UI at
<http://localhost:8000/docs>.

## Running with Docker

Build the image:

```powershell
docker build -t transaction-webhook .
```

Run it using the local `.env` file:

```powershell
docker run --rm `
  --name transaction-webhook `
  -p 8000:8000 `
  --env-file .env `
  transaction-webhook
```

Alternatively:

```powershell
docker compose up --build
```

## API usage

### Health check

```powershell
Invoke-RestMethod http://localhost:8000/
```

Example:

```json
{
  "status": "HEALTHY",
  "current_time": "2026-11-03T10:30:00Z"
}
```

The endpoint checks the database before returning `HEALTHY`. A database failure produces HTTP
`503 Service Unavailable`.

### Submit a transaction

```powershell
$body = @{
  transaction_id      = "txn_abc123def456"
  source_account      = "acc_user_789"
  destination_account = "acc_merchant_456"
  amount              = 1500
  currency            = "INR"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://localhost:8000/v1/webhooks/transactions `
  -ContentType application/json `
  -Body $body
```

The response is returned immediately with HTTP `202 Accepted`:

```json
{
  "transaction_id": "txn_abc123def456",
  "status": "PROCESSING",
  "duplicate": false
}
```

The status in this response is an asynchronous acknowledgement. The GET endpoint is the source of
truth for the persisted status.

### Retrieve transaction status

```powershell
Invoke-RestMethod http://localhost:8000/v1/transactions/txn_abc123def456
```

Before processing finishes, the status is `PROCESSING` and `processed_at` is `null`. After roughly
30 seconds, the response becomes:

```json
[
  {
    "transaction_id": "txn_abc123def456",
    "source_account": "acc_user_789",
    "destination_account": "acc_merchant_456",
    "amount": "1500.00",
    "currency": "INR",
    "status": "PROCESSED",
    "created_at": "2026-11-03T10:30:00Z",
    "processed_at": "2026-11-03T10:30:30Z"
  }
]
```

## Testing idempotency

Submit the same request body several times. The first response should contain
`"duplicate": false`, while every later response should contain `"duplicate": true`.

After processing finishes, this Supabase query should return exactly one row with `attempts = 1`:

```sql
SELECT transaction_id, status, attempts, created_at, processed_at
FROM public.transactions
WHERE transaction_id = 'txn_abc123def456';
```

This query should return no rows:

```sql
SELECT transaction_id, COUNT(*)
FROM public.transactions
GROUP BY transaction_id
HAVING COUNT(*) > 1;
```

## Running automated tests

The automated tests use an in-memory repository, so they do not require Supabase credentials:

```powershell
python -m pytest -q
ruff check .
```

The test suite covers:

- Health checks
- Webhook acceptance
- Duplicate delivery handling
- Transaction lookup
- Request validation
- Decimal amount preservation
- Successful worker completion

## Configuration

| Variable | Default | Description |
| --- | ---: | --- |
| `DATABASE_URL` | required | Supabase PostgreSQL connection URI |
| `AUTO_CREATE_SCHEMA` | `true` | Automatically create the table and indexes |
| `PROCESSING_DELAY_SECONDS` | `30` | Simulated external-operation duration |
| `WORKER_POLL_INTERVAL_SECONDS` | `0.5` | Delay between empty queue polls |
| `WORKER_BATCH_SIZE` | `10` | Maximum number of jobs claimed per poll |
| `WORKER_CONCURRENCY` | `10` | Maximum concurrent jobs per application process |
| `PROCESSING_LEASE_SECONDS` | `120` | Time before an interrupted job can be reclaimed |
| `MAX_PROCESSING_ATTEMPTS` | `5` | Attempts before a job is marked `FAILED` |
| `RETRY_BASE_SECONDS` | `5` | Base delay for exponential retries |
| `LOG_LEVEL` | `INFO` | Application logging level |

`PROCESSING_LEASE_SECONDS` should always remain comfortably larger than the longest expected
processing duration.

## Production considerations

For a larger production deployment, I would make the following changes:

- Run the worker as a separate deployment so API and worker capacity can scale independently.
- Replace automatic schema creation with versioned database migrations.
- Verify webhook signatures before accepting requests.
- Send `transaction_id` as the downstream provider's idempotency key.
- Add structured logs, queue-depth metrics, distributed tracing, and alerting.
- Add a dead-letter workflow for transactions that exhaust their retries.
- Add integration tests against an isolated PostgreSQL database.

The current implementation keeps API and worker deployment together for simplicity while retaining
durable storage, concurrency control, retries, and crash recovery.
