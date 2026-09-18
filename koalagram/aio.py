"""asyncio 기반 클라이언트.

동기판(`graphql.GraphQLClient`)과 같은 표면·같은 파싱을 쓰되 `AsyncSession` 으로
동시에 요청한다. 회전 프록시를 쓰면 **한 세션 안에서도 동시 요청마다 IP 가
바뀌므로**(실측 확인) 요청별로 세션을 새로 만들 필요가 없다.

    client = AsyncKoalagram(proxy="proxies.txt")
    async with client:
        medias = await client.fetch_many(urls, concurrency=24)
"""

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

from curl_cffi import requests

from .errors import (
    FetchError,
    GatingRuling,
    KoalagramError,
    MediaGatedError,
    MediaNotFoundError,
    RateLimitError,
)
from .graphql import (
    RATE_LIMIT_ERROR_CODE,
    GraphQLConfig,
    SessionTokens,
    _EQMC_RE,
    _LSD_RE,
    extract_shortcode,
    id_to_pk,
    is_login_redirect,
    parse_media,
)
from .analysis import analyze_media, resolve_groq_api_key
from .models import AnalysisResult, Media
from .proxy import ProxyPool
from .retry import (
    MAX_ATTEMPTS,
    VIEW_COUNT_MAX_ATTEMPTS,
    RetryStats,
    async_retry_on_rate_limit,
)
from .viewcount import EMBED_URL, ViewCountState, parse_view_count


class AsyncGraphQLClient:
    """로그아웃 GraphQL 클라이언트 (asyncio)."""

    def __init__(self, config: Optional[GraphQLConfig] = None, debug: bool = False,
                 max_clients: int = 256):
        self.config = config or GraphQLConfig()
        self.proxies = ProxyPool.build(self.config.proxy)
        self.debug = debug
        self.tokens = SessionTokens()
        self.retry_stats = RetryStats()
        self._max_clients = max_clients
        self._session: Optional[Any] = None
        self._token_lock = asyncio.Lock()

    @property
    def proxied(self) -> bool:
        return bool(self.proxies)

    # ---- 세션 ----

    @property
    def session(self):
        if self._session is None:
            self._session = self._new_session()
        return self._session

    def _new_session(self):
        kwargs: Dict[str, Any] = {
            "impersonate": self.config.impersonate,
            "max_clients": self._max_clients,
        }
        proxy = self.proxies.next()
        if proxy:
            kwargs["proxy"] = proxy
        return requests.AsyncSession(**kwargs)

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.close()
            except Exception:
                pass
            self._session = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    def reset(self) -> None:
        """다음 요청부터 새 세션/토큰을 쓴다."""
        self._session = None
        self.tokens = SessionTokens()

    # ---- 토큰 ----

    async def ensure_tokens(self) -> SessionTokens:
        if self.tokens.ready:
            return self.tokens
        async with self._token_lock:
            if self.tokens.ready:
                return self.tokens

            response = await self.session.get(self.config.base_url + "/",
                                              timeout=self.config.timeout)
            # 홈페이지 요청이라 path 가 "/" 인 건 정상이다.
            # /accounts/login 으로 튕긴 경우에만 차단으로 본다.
            if "/accounts/login" in str(response.url):
                raise RateLimitError("Home page redirected to login - rate limit exceeded")
            html = response.text

            lsd = ""
            match = _EQMC_RE.search(html)
            if match:
                try:
                    lsd = json.loads(match.group(1)).get("l") or ""
                except json.JSONDecodeError:
                    lsd = ""
            if not lsd:
                match = _LSD_RE.search(html)
                lsd = match.group(1) if match else ""
            if not lsd:
                raise FetchError("Could not obtain LSD token from instagram.com")

            # LSD 토큰은 IP 에 묶이지 않아 다른 IP 에서도 재사용된다 (실측 확인)
            self.tokens = SessionTokens(
                lsd=lsd, csrf=self.session.cookies.get("csrftoken") or "")
            if self.debug:
                print(f"[koalagram] tokens ready lsd={lsd[:10]}...")
            return self.tokens

    # ---- 질의 ----

    def _headers(self, shortcode: str) -> Dict[str, str]:
        headers = {
            "X-IG-App-ID": self.config.app_id,
            "X-ASBD-ID": self.config.asbd_id,
            "X-IG-WWW-Claim": "0",
            "Origin": self.config.base_url,
            "Accept": "*/*",
            "X-FB-Friendly-Name": self.config.friendly_name,
            "X-FB-LSD": self.tokens.lsd,
            "X-Requested-With": "XMLHttpRequest",
            "Referer": f"{self.config.base_url}/p/{shortcode}/",
        }
        if self.tokens.csrf:
            headers["X-CSRFToken"] = self.tokens.csrf
        return headers

    async def _raw_media_once(self, shortcode: str) -> Dict[str, Any]:
        await self.ensure_tokens()
        payload = {
            "lsd": self.tokens.lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": self.config.friendly_name,
            "server_timestamps": "true",
            "variables": json.dumps({"media_id": str(id_to_pk(shortcode))},
                                    separators=(",", ":")),
            "doc_id": self.config.doc_id,
        }
        try:
            response = await self.session.post(
                self.config.graphql_url,
                headers=self._headers(shortcode),
                data=payload,
                timeout=self.config.timeout,
            )
        except Exception as e:
            raise FetchError(f"GraphQL request failed: {type(e).__name__}: {e}")

        if is_login_redirect(response.url):
            raise RateLimitError("GraphQL redirected to login - rate limit exceeded")
        if response.status_code == 429:
            raise RateLimitError("GraphQL returned HTTP 429")
        if response.status_code != 200:
            raise FetchError(f"GraphQL returned HTTP {response.status_code}")

        try:
            data = response.json()
        except Exception:
            raise FetchError("GraphQL returned a non-JSON response")

        errors = data.get("errors") or []
        for error in errors:
            message = str(error.get("message", ""))
            if error.get("code") == RATE_LIMIT_ERROR_CODE or "rate limit" in message.lower():
                raise RateLimitError(
                    f"{message or 'Rate limit exceeded'} (code={error.get('code')})")

        # GraphQL 은 data 와 errors 를 함께 주기도 한다 (차단/게이팅 게시물에서
        # 일부 필드만 실패). data 가 있으면 그대로 쓰고, 없을 때만 오류로 본다.
        media = (data.get("data") or {}).get("xig_polaris_media")
        if not media:
            if errors:
                raise FetchError(
                    f"[{shortcode}] GraphQL error: "
                    f"{str(errors[0].get('message', ''))[:120]}")
            raise MediaNotFoundError(
                f"[{shortcode}] post does not exist (removed or private account)")
        info = media.get("if_not_gated_logged_out")
        if not info:
            raise MediaGatedError(shortcode, GatingRuling.from_dict(media.get("gating_ruling")))
        return info

    async def raw_media(self, shortcode: str) -> Dict[str, Any]:
        def log_retry(attempt: int, delay: float, error: Exception) -> None:
            print(f"[koalagram] 재시도 {attempt}/{MAX_ATTEMPTS} "
                  f"({delay:.0f}초 후) [{shortcode}] {str(error)[:60]}", flush=True)

        return await async_retry_on_rate_limit(
            lambda: self._raw_media_once(shortcode),
            proxied=self.proxied,
            on_retry=log_retry,
            before_retry=self.reset,
            stats=self.retry_stats,
        )

    async def fetch(self, url: str) -> Media:
        shortcode = extract_shortcode(url)
        return parse_media(await self.raw_media(shortcode), shortcode=shortcode)

    # ---- 조회수 (embed) ----

    async def _view_once(self, shortcode: str) -> Optional[int]:
        response = await self.session.get(EMBED_URL.format(shortcode=shortcode),
                                          timeout=self.config.timeout)
        if is_login_redirect(response.url):
            raise RateLimitError("embed redirected to login (rate limit exceeded)")
        if response.status_code != 200:
            raise FetchError(f"embed returned HTTP {response.status_code}")
        return parse_view_count(response.text)

    async def view(self, shortcode: str) -> Optional[int]:
        """조회수. 실패하면 예외 없이 None (이미 받은 GraphQL 결과를 살린다)."""
        try:
            return await async_retry_on_rate_limit(
                lambda: self._view_once(shortcode),
                proxied=self.proxied,
                max_attempts=VIEW_COUNT_MAX_ATTEMPTS,
            )
        except Exception:
            return None


@dataclass
class FetchResult:
    """fetch_many 결과 한 건."""
    url: str
    media: Optional[Media] = None
    error: Optional[BaseException] = None

    @property
    def ok(self) -> bool:
        return self.media is not None


class AsyncKoalagram:
    """asyncio 기반 Koalagram.

        client = AsyncKoalagram(proxy="proxies.txt")
        async with client:
            results = await client.fetch_many(urls, concurrency=24)
    """

    def __init__(self, proxy: Any = None, debug: bool = False,
                 groq_api_key: Optional[str] = None,
                 config: Optional[GraphQLConfig] = None, max_clients: int = 256):
        from dataclasses import replace
        cfg = config or GraphQLConfig()
        if proxy is not None:
            cfg = replace(cfg, proxy=proxy)
        self.client = AsyncGraphQLClient(config=cfg, debug=debug, max_clients=max_clients)
        self.debug = debug
        # 생략하면 GROQ_API_KEY 환경변수를 쓴다
        self.groq_api_key = resolve_groq_api_key(groq_api_key)

    @property
    def proxies(self) -> ProxyPool:
        return self.client.proxies

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.close()

    async def close(self) -> None:
        await self.client.close()

    async def fetch(self, url: str, with_view_count: bool = False) -> Media:
        media = await self.client.fetch(url)
        media._client = self      # media.analyze() 가 쓸 수 있도록
        if with_view_count and media.view_count is None and media.is_video:
            media.view_count = await self.client.view(media.shortcode)
        return media

    async def analyze(self, media: Media) -> AnalysisResult:
        """캡션에서 요약과 장소를 뽑는다 (Groq API 키 필요)."""
        return await analyze_media(media, self)

    async def view(self, media: Media) -> Optional[int]:
        """조회수를 가져와 view_count 에 채운다 (embed)."""
        if media.view_count is not None:
            return media.view_count
        if not media.is_video or not media.shortcode:
            return None
        media.view_count = await self.client.view(media.shortcode)
        return media.view_count

    async def fetch_many(self, urls: Sequence[str], concurrency: int = 24,
                         with_view_count: bool = False) -> List[FetchResult]:
        """여러 건을 동시에 가져온다. 실패는 FetchResult.error 에 담긴다."""
        semaphore = asyncio.Semaphore(concurrency)

        async def one(url: str) -> FetchResult:
            async with semaphore:
                try:
                    return FetchResult(url=url, media=await self.fetch(url, with_view_count))
                except BaseException as e:      # noqa: BLE001 - 호출부가 분류한다
                    return FetchResult(url=url, error=e)

        return list(await asyncio.gather(*(one(u) for u in urls)))
