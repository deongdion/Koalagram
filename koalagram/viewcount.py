"""조회수 보강 (embed 표면 전용).

로그아웃 GraphQL 은 `play_count` 를 주지 않는다. 조회수를 주는 곳은
embed 페이지(`/p/<code>/embed/captioned/`)의 `contextJSON` 안
`video_view_count` 뿐이다.

embed 는 GraphQL 보다 한도가 빡빡해서 (실측 약 22,000건 뒤 로그인 리다이렉트)
모든 게시물마다 치면 금방 막힌다. 그래서:

  - 게시물당 **최대 1회**만 시도한다
  - 한 번이라도 막히면(`disable_on_failure`) 이후로는 **아예 시도하지 않는다**

조회수를 못 얻어도 영상 URL 등 나머지는 GraphQL 로 이미 확보돼 있으므로
그대로 진행된다.
"""

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Optional

from curl_cffi import requests

from .errors import RateLimitError
from .proxy import ProxyPool
from .retry import VIEW_COUNT_MAX_ATTEMPTS, RetryStats, retry_on_rate_limit

EMBED_URL = "https://www.instagram.com/p/{shortcode}/embed/captioned/"
_CONTEXT_JSON_RE = re.compile(r'"contextJSON"\s*:\s*("(?:[^"\\]|\\.)*")')


@dataclass
class ViewCountState:
    """보강 시도 누적 상태."""
    attempts: int = 0
    hits: int = 0
    misses: int = 0
    disabled: bool = False
    disabled_reason: str = ""

    def record_hit(self) -> None:
        self.attempts += 1
        self.hits += 1

    def record_miss(self) -> None:
        self.attempts += 1
        self.misses += 1

    def disable(self, reason: str) -> None:
        self.disabled = True
        self.disabled_reason = reason

    def reset(self) -> None:
        self.disabled = False
        self.disabled_reason = ""


def _is_login_redirect(url) -> bool:
    path = urllib.parse.urlparse(str(url)).path
    return path.startswith("/accounts/login")


def parse_view_count(page_html: str) -> Optional[int]:
    """embed HTML 에서 video_view_count 를 꺼낸다."""
    match = _CONTEXT_JSON_RE.search(page_html)
    if not match:
        return None
    try:
        context = json.loads(json.loads(match.group(1)))
    except (json.JSONDecodeError, TypeError):
        return None
    media = (context.get("gql_data") or {}).get("shortcode_media") or {}
    value = media.get("video_view_count")
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


@dataclass
class ViewCountFetcher:
    """embed 에서 조회수만 가져온다.

    disable_on_failure=True(기본) 면 첫 실패 이후 영구 비활성화된다.
    """
    impersonate: str = "chrome"
    timeout: int = 30
    proxy: Any = None
    disable_on_failure: bool = True
    proxies: Any = field(default=None, repr=False)
    state: ViewCountState = field(default_factory=ViewCountState)
    retry_stats: RetryStats = field(default_factory=RetryStats)
    session: Any = field(default=None, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self):
        self.proxies = ProxyPool.build(self.proxy)
        if self.session is None:
            self.session = self._new()

    def _new(self):
        kwargs = {"impersonate": self.impersonate}
        proxy = self.proxies.next()
        if proxy:
            kwargs["proxy"] = proxy
        return requests.Session(**kwargs)

    @property
    def proxied(self) -> bool:
        return bool(self.proxies)

    def _request_session(self):
        """프록시를 쓰면 요청마다 새 세션(=새 IP)."""
        return self._new() if self.proxied else self.session

    @property
    def enabled(self) -> bool:
        return not self.state.disabled

    def fetch(self, shortcode: str, debug: bool = False) -> Optional[int]:
        """조회수를 가져온다.

        조회수는 부가 정보라 VIEW_COUNT_MAX_ATTEMPTS 회만 짧게 재시도한다.
        실패해도 예외를 던지지 않고 None 을 돌려준다 - 호출부가 이미 확보한
        GraphQL 결과(영상 URL 등)를 그대로 쓸 수 있어야 하기 때문이다.
        전부 소진하면(그리고 disable_on_failure 면) 이후 호출은 요청 없이 None.
        """
        with self._lock:
            if self.state.disabled:
                return None

        def log_retry(attempt: int, delay: float, error: RateLimitError) -> None:
            print(f"[koalagram] embed 레이트리밋 - {delay / 60:.1f}분 대기 후 재시도 "
                  f"({attempt}/{VIEW_COUNT_MAX_ATTEMPTS}) [{shortcode}]", flush=True)

        try:
            view_count = retry_on_rate_limit(
                lambda: self._fetch_once(shortcode),
                proxied=self.proxied,
                max_attempts=VIEW_COUNT_MAX_ATTEMPTS,
                on_retry=log_retry,
                before_retry=self._new_session,
                stats=self.retry_stats,
            )
        except RateLimitError as e:
            self._fail(str(e), debug)
            return None
        except Exception as e:
            self._fail(f"{type(e).__name__}: {e}", debug)
            return None

        with self._lock:
            if view_count is None:
                # 차단 게시물/이미지 게시물은 원래 조회수가 없다 - 비활성화 대상 아님
                self.state.record_miss()
            else:
                self.state.record_hit()
        if debug:
            print(f"[koalagram] view_count({shortcode}) = {view_count}")
        return view_count

    def _fetch_once(self, shortcode: str) -> Optional[int]:
        """embed 1회 조회. 레이트리밋이면 RateLimitError."""
        response = self._request_session().get(
            EMBED_URL.format(shortcode=shortcode), timeout=self.timeout)
        if _is_login_redirect(response.url):
            raise RateLimitError("embed redirected to login (rate limit exceeded)")
        if response.status_code != 200:
            raise RuntimeError(f"embed returned HTTP {response.status_code}")
        return parse_view_count(response.text)

    def _new_session(self) -> None:
        self.session = self._new()

    def _fail(self, reason: str, debug: bool) -> None:
        with self._lock:
            self.state.record_miss()
            if self.disable_on_failure:
                self.state.disable(reason)
        if debug:
            print(f"[koalagram] 조회수 보강 중단: {reason}")
