"""레이트리밋 재시도.

두 표면 모두 익명 조회 한도가 있다:
  - GraphQL : HTTP 200 + `errors[].code == 1675004` (실측 약 15,000건)
  - embed   : `/accounts/login/?...&is_from_rle` 로 리다이렉트 (실측 약 22,000건)

회복은 빠르지 않다 (실측 13분 이상 지속). 그래서 간격을 분 단위로 잡고,
매 시도마다 세션과 토큰을 새로 발급한다.
"""

import asyncio
import random
import time
from dataclasses import dataclass
from typing import Awaitable, Callable, Optional, TypeVar

from .errors import FetchError, RateLimitError

T = TypeVar("T")

MAX_ATTEMPTS = 10
# 조회수는 있으면 좋은 부가 정보일 뿐이라 오래 매달리지 않는다.
# (GraphQL 로 이미 받은 영상 URL 을 붙잡고 몇 시간씩 기다리면 안 된다)
VIEW_COUNT_MAX_ATTEMPTS = 2
# 직접 연결: 한 IP 로 계속 때리므로 회복을 기다려야 한다. 누적 약 2.4시간.
DELAYS = (60, 120, 300, 600, 900, 1200, 1800, 1800, 1800)
# 프록시(요청마다 IP 회전): 다음 시도는 다른 IP 라 오래 기다릴 이유가 없다.
PROXY_DELAYS = (1, 2, 3, 5, 5, 10, 10, 15, 15)
JITTER = 0.1   # 대기시간 ±10%


def delay_for(attempt: int, proxied: bool = False) -> float:
    """attempt(1-base) 실패 뒤 기다릴 초."""
    table = PROXY_DELAYS if proxied else DELAYS
    base = float(table[min(attempt, len(table)) - 1])
    return max(0.0, base * (1.0 + random.uniform(-JITTER, JITTER)))


@dataclass
class RetryStats:
    """재시도 누적 기록."""
    attempts: int = 0
    retries: int = 0
    slept_seconds: float = 0.0
    last_error: str = ""


def retry_on_rate_limit(
    operation: Callable[[], T],
    *,
    proxied: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
    on_retry: Optional[Callable[[int, float, Exception], None]] = None,
    before_retry: Optional[Callable[[], None]] = None,
    sleep: Callable[[float], None] = time.sleep,
    stats: Optional[RetryStats] = None,
) -> T:
    """RateLimitError 가 나면 정해진 간격으로 최대 MAX_ATTEMPTS 회까지 재시도한다.

    RateLimitError 와 FetchError(네트워크/프록시 오류)를 재시도한다.
    proxied=True 면 짧은 간격을 쓴다 (다음 시도는 다른 IP 이므로).

    on_retry(attempt, delay, error) : 대기 직전 호출 (로그용)
    before_retry()                  : 대기 후 재시도 직전 호출 (세션 재발급용)
    다른 예외는 그대로 올려보낸다.
    """
    stats = stats if stats is not None else RetryStats()
    last: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        stats.attempts += 1
        try:
            return operation()
        except (RateLimitError, FetchError) as e:
            # FetchError: 프록시 연결 끊김/서버 일시 오류 등도 다시 시도한다
            last = e
            stats.last_error = str(e)
            if attempt >= max_attempts:
                break
            delay = delay_for(attempt, proxied)
            if on_retry:
                on_retry(attempt, delay, e)
            sleep(delay)
            stats.slept_seconds += delay
            stats.retries += 1
            if before_retry:
                before_retry()

    if isinstance(last, FetchError):
        raise FetchError(f"Failed after {max_attempts} attempts: {last}")
    raise RateLimitError(
        f"Rate limited after {max_attempts} attempts "
        f"({stats.slept_seconds / 60:.1f}분 대기). 마지막 오류: {last}")


async def async_retry_on_rate_limit(
    operation: Callable[[], "Awaitable[T]"],
    *,
    proxied: bool = False,
    max_attempts: int = MAX_ATTEMPTS,
    on_retry: Optional[Callable[[int, float, Exception], None]] = None,
    before_retry: Optional[Callable[[], None]] = None,
    stats: Optional[RetryStats] = None,
) -> T:
    """retry_on_rate_limit 의 asyncio 판."""
    stats = stats if stats is not None else RetryStats()
    last: Optional[Exception] = None

    for attempt in range(1, max_attempts + 1):
        stats.attempts += 1
        try:
            return await operation()
        except (RateLimitError, FetchError) as e:
            last = e
            stats.last_error = str(e)
            if attempt >= max_attempts:
                break
            delay = delay_for(attempt, proxied)
            if on_retry:
                on_retry(attempt, delay, e)
            await asyncio.sleep(delay)
            stats.slept_seconds += delay
            stats.retries += 1
            if before_retry:
                before_retry()

    if isinstance(last, FetchError):
        raise FetchError(f"Failed after {max_attempts} attempts: {last}")
    raise RateLimitError(
        f"Rate limited after {max_attempts} attempts "
        f"({stats.slept_seconds / 60:.1f}분 대기). 마지막 오류: {last}")
