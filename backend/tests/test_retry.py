from app.models import RetryStrategy
from app.services.retry import compute_delay_seconds


def test_fixed_delay_is_constant_across_attempts():
    delays = [compute_delay_seconds(RetryStrategy.FIXED, n, base_delay_seconds=10, multiplier=2.0, max_delay_seconds=1000) for n in (1, 2, 3)]
    assert delays == [10, 10, 10]


def test_linear_delay_grows_by_attempt_number():
    assert compute_delay_seconds(RetryStrategy.LINEAR, 1, 10, 2.0, 1000) == 10
    assert compute_delay_seconds(RetryStrategy.LINEAR, 3, 10, 2.0, 1000) == 30


def test_exponential_delay_doubles_each_attempt():
    assert compute_delay_seconds(RetryStrategy.EXPONENTIAL, 1, 5, 2.0, 1000) == 5
    assert compute_delay_seconds(RetryStrategy.EXPONENTIAL, 2, 5, 2.0, 1000) == 10
    assert compute_delay_seconds(RetryStrategy.EXPONENTIAL, 4, 5, 2.0, 1000) == 40


def test_delay_is_capped_at_max_delay():
    assert compute_delay_seconds(RetryStrategy.EXPONENTIAL, 10, 5, 2.0, max_delay_seconds=60) == 60