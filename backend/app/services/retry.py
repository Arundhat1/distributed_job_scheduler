"""
Pure functions computing the delay before the next retry attempt.
Kept side-effect free and unit-testable in isolation from the DB/worker.
"""
from app.models import RetryStrategy


def compute_delay_seconds(
    strategy: RetryStrategy,
    attempt_number: int,  # the attempt that just failed (1-indexed)
    base_delay_seconds: int,
    multiplier: float,
    max_delay_seconds: int,
) -> int:
    if strategy == RetryStrategy.FIXED:
        delay = base_delay_seconds
    elif strategy == RetryStrategy.LINEAR:
        delay = base_delay_seconds * attempt_number
    elif strategy == RetryStrategy.EXPONENTIAL:
        delay = base_delay_seconds * (multiplier ** (attempt_number - 1))
    else:
        raise ValueError(f"Unknown retry strategy: {strategy}")

    return int(min(delay, max_delay_seconds))