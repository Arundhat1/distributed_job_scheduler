"""
Standalone worker process.

Run multiple instances of this (different --name) against the same API
to get real horizontal scaling; the atomic claim endpoint guarantees no
two instances ever execute the same job.

    python scripts/run_worker.py --name worker-1 --queues emails,reports --concurrency 4

Lifecycle per job: claim -> POST .../start -> run handler -> POST .../result
Graceful shutdown: SIGTERM/SIGINT stop new claims immediately and wait
(up to --drain-timeout seconds) for in-flight jobs to finish before exit.
"""
import argparse
import asyncio
import os
import random
import signal
import socket
import time
import traceback

import httpx

API_BASE = os.environ.get("SCHEDULER_API_URL", "http://localhost:8000")


# ---------------------------------------------------------------------
# Job handler registry. Real deployments would import task modules here
# (Celery-style @task decorators) keyed by Job.name.
# ---------------------------------------------------------------------

async def handler_send_email(payload: dict) -> dict:
    await asyncio.sleep(random.uniform(0.2, 1.0))
    if payload.get("simulate_failure"):
        raise RuntimeError("SMTP connection refused")
    return {"sent_to": payload.get("to")}


async def handler_generate_report(payload: dict) -> dict:
    await asyncio.sleep(random.uniform(0.5, 2.0))
    return {"rows_processed": payload.get("rows", 0)}


async def default_handler(payload: dict) -> dict:
    await asyncio.sleep(random.uniform(0.1, 0.5))
    return {"echo": payload}


HANDLERS = {
    "send_email": handler_send_email,
    "generate_report": handler_generate_report,
}


class Worker:
    def __init__(self, name: str, queues: list[str], concurrency: int, token: str, drain_timeout: int):
        self.name = name
        self.queues = queues
        self.concurrency = concurrency
        self.token = token
        self.drain_timeout = drain_timeout
        self.worker_id: int | None = None
        self.semaphore = asyncio.Semaphore(concurrency)
        self.active_jobs = 0
        self.shutting_down = False
        self.in_flight: set[asyncio.Task] = set()
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.AsyncClient(base_url=API_BASE, headers=headers, timeout=30.0)

    async def register(self, max_attempts: int = 10, base_delay: float = 1.0):
        """
        Retries registration with exponential backoff instead of crashing
        the whole process on the first connection blip (DNS not yet
        ready, API still starting, momentary network drop). Only gives
        up after max_attempts, at which point the process exits and lets
        the container's `restart: on-failure` policy take over.
        """
        last_exc: Exception | None = None
        for attempt in range(1, max_attempts + 1):
            try:
                resp = await self.client.post(
                    "/api/v1/workers/register",
                    json={
                        "name": self.name,
                        "hostname": socket.gethostname(),
                        "pid": os.getpid(),
                        "queues": self.queues,
                        "concurrency_limit": self.concurrency,
                    },
                )
                resp.raise_for_status()
                self.worker_id = resp.json()["id"]
                print(f"[{self.name}] registered as worker_id={self.worker_id}, watching queues={self.queues}")
                return
            except Exception as e:
                last_exc = e
                delay = min(base_delay * (2 ** (attempt - 1)), 30.0)
                print(f"[{self.name}] register attempt {attempt}/{max_attempts} failed: {e!r}, retrying in {delay:.1f}s")
                await asyncio.sleep(delay)

        raise RuntimeError(f"[{self.name}] failed to register after {max_attempts} attempts") from last_exc

    async def heartbeat_loop(self):
        while True:
            try:
                status = "draining" if self.shutting_down else ("busy" if self.active_jobs > 0 else "idle")
                await self.client.post(f"/api/v1/workers/{self.worker_id}/heartbeat", json={"status": status, "active_jobs": self.active_jobs})
            except Exception as e:
                print(f"[{self.name}] heartbeat failed: {e}")
            await asyncio.sleep(5)

    async def poll_loop(self):
        while not self.shutting_down:
            free_slots = self.concurrency - self.active_jobs
            if free_slots > 0:
                try:
                    resp = await self.client.post(
                        "/api/v1/workers/claim",
                        json={"worker_id": self.worker_id, "queue_names": self.queues, "max_jobs": free_slots},
                    )
                    resp.raise_for_status()
                    jobs = resp.json()
                    for job in jobs:
                        task = asyncio.create_task(self.execute_job(job))
                        self.in_flight.add(task)
                        task.add_done_callback(self.in_flight.discard)
                except Exception as e:
                    print(f"[{self.name}] claim failed: {e}")
            await asyncio.sleep(1.0)

    async def execute_job(self, job: dict):
        self.active_jobs += 1
        job_id = job["id"]
        try:
            await self.client.post(f"/api/v1/workers/{self.worker_id}/jobs/{job_id}/start")
            handler = HANDLERS.get(job["name"], default_handler)
            start = time.perf_counter()
            try:
                result = await asyncio.wait_for(handler(job["payload"]), timeout=job.get("timeout_seconds", 300))
                await self.client.post(
                    f"/api/v1/workers/{self.worker_id}/jobs/{job_id}/result",
                    json={"status": "succeeded", "result": result},
                )
                print(f"[{self.name}] job {job_id} ({job['name']}) succeeded in {time.perf_counter()-start:.2f}s")
            except asyncio.TimeoutError:
                await self.client.post(
                    f"/api/v1/workers/{self.worker_id}/jobs/{job_id}/result",
                    json={"status": "timed_out", "error_message": "Job exceeded timeout_seconds"},
                )
                print(f"[{self.name}] job {job_id} timed out")
            except Exception as e:
                await self.client.post(
                    f"/api/v1/workers/{self.worker_id}/jobs/{job_id}/result",
                    json={"status": "failed", "error_message": str(e), "error_stacktrace": traceback.format_exc()},
                )
                print(f"[{self.name}] job {job_id} failed: {e}")
        finally:
            self.active_jobs -= 1

    async def run(self):
        await self.register()
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.shutdown()))

        await asyncio.gather(self.poll_loop(), self.heartbeat_loop())

    async def shutdown(self):
        if self.shutting_down:
            return
        print(f"[{self.name}] shutdown signal received, draining {len(self.in_flight)} in-flight job(s)...")
        self.shutting_down = True
        try:
            await asyncio.wait_for(asyncio.gather(*self.in_flight, return_exceptions=True), timeout=self.drain_timeout)
        except asyncio.TimeoutError:
            print(f"[{self.name}] drain timeout exceeded, exiting with jobs still in-flight (they will time out and retry)")
        await self.client.aclose()
        os._exit(0)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", required=True)
    parser.add_argument("--queues", required=True, help="comma-separated queue names")
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--token", default=os.environ.get("SCHEDULER_TOKEN", ""))
    parser.add_argument("--drain-timeout", type=int, default=30)
    args = parser.parse_args()

    worker = Worker(args.name, args.queues.split(","), args.concurrency, args.token, args.drain_timeout)
    asyncio.run(worker.run())


if __name__ == "__main__":
    main()