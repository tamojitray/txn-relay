import asyncio
import logging

from app.config import Settings
from app.repository import Job, Repository

logger = logging.getLogger(__name__)


class TransactionWorker:
    def __init__(self, repository: Repository, settings: Settings) -> None:
        self._repository = repository
        self._settings = settings
        self._stop_event = asyncio.Event()
        self._runner: asyncio.Task | None = None
        self._active: set[asyncio.Task] = set()
        self._capacity = asyncio.Semaphore(settings.worker_concurrency)

    async def start(self) -> None:
        self._runner = asyncio.create_task(self._run(), name="transaction-worker")

    async def stop(self) -> None:
        self._stop_event.set()
        if self._runner:
            await self._runner
        if self._active:
            await asyncio.gather(*self._active, return_exceptions=True)

    async def _run(self) -> None:
        logger.info("Transaction worker started")
        while not self._stop_event.is_set():
            try:
                available = self._settings.worker_concurrency - len(self._active)
                if available > 0:
                    jobs = await self._repository.claim_jobs(
                        batch_size=min(self._settings.worker_batch_size, available),
                        lease_seconds=self._settings.processing_lease_seconds,
                        max_attempts=self._settings.max_processing_attempts,
                    )
                    for job in jobs:
                        task = asyncio.create_task(
                            self.process_job(job),
                            name=f"job-{job.transaction_id}",
                        )
                        self._active.add(task)
                        task.add_done_callback(self._active.discard)
                    if jobs:
                        continue
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Worker polling failed")

            try:
                await asyncio.wait_for(
                    self._stop_event.wait(),
                    timeout=self._settings.worker_poll_interval_seconds,
                )
            except TimeoutError:
                pass
        logger.info("Transaction worker stopped accepting jobs")

    async def process_job(self, job: Job) -> None:
        async with self._capacity:
            try:
                # Simulates the slow external payment-processor call from the brief.
                await asyncio.sleep(self._settings.processing_delay_seconds)
                await self._repository.mark_processed(job.transaction_id)
                logger.info("Processed transaction %s", job.transaction_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                retry_seconds = self._settings.retry_base_seconds * (2 ** (job.attempts - 1))
                logger.exception("Transaction %s failed", job.transaction_id)
                await self._repository.mark_failed(
                    transaction_id=job.transaction_id,
                    attempts=job.attempts,
                    max_attempts=self._settings.max_processing_attempts,
                    error=str(exc),
                    retry_seconds=retry_seconds,
                )
