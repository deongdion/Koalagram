"""인스타그램 로그아웃 GraphQL 클라이언트.

추출 방식은 yt-dlp 의 Instagram extractor 를 참고했다
(shortcode<->pk 변환, persisted query, video_versions / DASH 처리).

`POST /api/graphql` 에 고정 doc_id 로 질의하면 로그인 없이 게시물 정보를 받는다.
embed 표면(`/p/<code>/embed/`)과 달리:
  - 저작권 차단(copyright_blocked) 게시물도 video_versions 를 준다
  - full_name / taken_at / location / 썸네일 후보를 준다
  - 이미지/캐러셀/릴스를 한 경로로 처리한다
  - 요청량 여유가 더 크다 (실측 32스레드 55req/s 로 약 15,000건까지)
다만 조회수(view_count)는 주지 않는다.
"""

import json
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from curl_cffi import requests

from .errors import (
    FetchError,
    GatingRuling,
    InvalidURLError,
    MediaGatedError,
    MediaNotFoundError,
    RateLimitError,
)
from .retry import MAX_ATTEMPTS, RetryStats, retry_on_rate_limit
from .proxy import ProxyPool
from .models import (
    MEDIA_TYPE_CAROUSEL,
    MEDIA_TYPE_VIDEO,
    File,
    FileType,
    Location,
    Media,
    MediaType,
    Thumbnail,
    User,
    VideoVersion,
)

# shortcode 는 이 알파벳의 base64 표현이다
_ENCODING_CHARS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
_SHORTCODE_MARKERS = ("/p/", "/reel/", "/reels/", "/tv/")
_SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]{5,}$")
_EQMC_RE = re.compile(r'<script[^>]*id="__eqmc"[^>]*>(\{.+?\})</script>')
_LSD_RE = re.compile(r'\["LSD",\[\],\{"token":"([^"]+)"')

# 레이트리밋 초과 시 GraphQL 이 HTTP 200 과 함께 돌려주는 에러 코드
RATE_LIMIT_ERROR_CODE = 1675004


@dataclass(frozen=True)
class GraphQLConfig:
    """GraphQL 질의에 필요한 고정 값들."""
    base_url: str = "https://www.instagram.com"
    doc_id: str = "27130156389949648"
    friendly_name: str = "PolarisLoggedOutDesktopWWWPostRootContentQuery"
    app_id: str = "936619743392459"
    asbd_id: str = "359341"
    impersonate: str = "chrome"
    timeout: int = 60
    # 프록시. 문자열 하나, proxies.txt 경로, 목록 중 아무거나.
    # 설정하면 요청마다 새 세션을 만든다 (같은 세션은 IP 가 고정되므로).
    proxy: Optional[object] = None

    @property
    def graphql_url(self) -> str:
        return f"{self.base_url}/api/graphql"

    @property
    def ruling_url(self) -> str:
        return f"{self.base_url}/api/v1/web/get_ruling_for_content/"


@dataclass
class SessionTokens:
    """홈페이지 1회 방문으로 확보하는 토큰들."""
    lsd: str = ""
    csrf: str = ""

    @property
    def ready(self) -> bool:
        return bool(self.lsd)


@dataclass
class ClientState:
    """요청 누적 상태와 스로틀링 감지.

    레이트리밋은 HTTP 200 + `errors[].code == 1675004` 로 명확히 온다
    (`{"errors":[{"message":"Rate limit exceeded","severity":"CRITICAL",
    "code":1675004}],"extensions":{"is_final":true}}`). 삭제된 게시물은
    `data.xig_polaris_media == null` 이라 서로 구분된다.

    연속 not-found 임계치는 에러 페이로드 없이 조용히 빈 응답만 오는
    형태의 스로틀링에 대비한 보조 장치다. 실측(32스레드, 55req/s)에서
    진짜 not-found 는 최대 2연속까지만 나왔다.

    참고: 관측된 한도는 약 15,000건이었다.
    """
    requests: int = 0
    consecutive_not_found: int = 0
    rate_limit_hits: int = 0
    not_found_threshold: int = 8   # 관측 최대치(2)의 4배

    def record_hit(self) -> None:
        """게시물이든 게이팅이든 '진짜 응답'을 받았다."""
        self.requests += 1
        self.consecutive_not_found = 0

    def record_not_found(self) -> int:
        self.requests += 1
        self.consecutive_not_found += 1
        return self.consecutive_not_found

    def record_rate_limit(self) -> None:
        self.requests += 1
        self.rate_limit_hits += 1

    @property
    def looks_rate_limited(self) -> bool:
        return self.consecutive_not_found >= self.not_found_threshold

    def reset(self) -> None:
        self.consecutive_not_found = 0


# --------------------------------------------------------------------------
# shortcode <-> pk (네트워크 불필요)
# --------------------------------------------------------------------------

def id_to_pk(shortcode: str) -> int:
    """shortcode -> 숫자 media id."""
    if len(shortcode) > 28:
        shortcode = shortcode[:-28]
    value = 0
    for ch in shortcode:
        try:
            value = value * 64 + _ENCODING_CHARS.index(ch)
        except ValueError:
            raise InvalidURLError(f"Invalid shortcode character: {ch!r}")
    return value


def pk_to_id(media_id) -> str:
    """숫자 media id -> shortcode."""
    pk = int(str(media_id).split("_")[0])
    if pk == 0:
        return _ENCODING_CHARS[0]
    out = ""
    while pk:
        pk, rem = divmod(pk, 64)
        out = _ENCODING_CHARS[rem] + out
    return out


def extract_shortcode(url: str) -> str:
    """게시물 URL(또는 shortcode 자체)에서 shortcode 를 뽑는다."""
    url = (url or "").strip()
    for marker in _SHORTCODE_MARKERS:
        if marker in url:
            tail = url.split(marker, 1)[1]
            code = tail.split("?")[0].split("#")[0].strip("/").split("/")[0]
            if code:
                return code
    if _SHORTCODE_RE.match(url):
        return url
    raise InvalidURLError(f"Invalid Instagram URL: {url!r}")


def is_login_redirect(url: str) -> bool:
    """로그인 페이지로 튕기면 익명 조회 한도를 넘긴 것.

    한도 초과 시 최종 URL: /accounts/login/?next=...&is_from_rle
    """
    path = urllib.parse.urlparse(str(url)).path
    return path.startswith("/accounts/login") or path == "/"


# --------------------------------------------------------------------------
# 클라이언트
# --------------------------------------------------------------------------

class GraphQLClient:
    """로그인 없이 게시물을 조회한다. 토큰은 한 번 받아 재사용한다."""

    def __init__(self, config: Optional[GraphQLConfig] = None,
                 session=None, debug: bool = False):
        self.config = config or GraphQLConfig()
        self.retry_stats = RetryStats()
        self.proxies = ProxyPool.build(self.config.proxy)
        self.session = session or self.new_session()
        self.tokens = SessionTokens()
        self.state = ClientState()
        self.debug = debug
        self._lock = threading.Lock()

    @property
    def proxied(self) -> bool:
        return bool(self.proxies)

    def new_session(self):
        """세션 하나를 만든다. 풀에서 다음 프록시를 받아 붙인다."""
        kwargs = {"impersonate": self.config.impersonate}
        proxy = self.proxies.next()
        if proxy:
            kwargs["proxy"] = proxy
        return requests.Session(**kwargs)

    def _request_session(self):
        """요청 1건에 쓸 세션.

        프록시를 쓸 때는 매번 새 세션을 만든다. 같은 세션은 커넥션을 재사용해
        IP 가 고정되기 때문이다 (실측: 같은 세션 6회 -> 전부 같은 IP).
        """
        return self.new_session() if self.proxied else self.session

    # ---- 토큰 ----

    def ensure_tokens(self) -> SessionTokens:
        if self.tokens.ready:
            return self.tokens

        session = self._request_session()
        response = session.get(self.config.base_url + "/", timeout=self.config.timeout)
        if is_login_redirect(response.url) and "/accounts/login" in str(response.url):
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
        self.tokens = SessionTokens(lsd=lsd, csrf=session.cookies.get("csrftoken") or "")
        if self.debug:
            print(f"[koalagram] tokens ready lsd={lsd[:10]}... csrf={self.tokens.csrf[:10]}...")
        return self.tokens

    def reset(self) -> None:
        """세션과 토큰을 새로 발급한다 (레이트리밋 복구용)."""
        self.session = self.new_session()
        self.tokens = SessionTokens()
        self.state.reset()

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

    def raw_media(self, shortcode: str) -> Dict[str, Any]:
        """게시물 dict 를 가져온다. 레이트리밋이면 정해진 간격으로 재시도한다."""

        def log_retry(attempt: int, delay: float, error: RateLimitError) -> None:
            print(f"[koalagram] 레이트리밋 - {delay / 60:.1f}분 대기 후 재시도 "
                  f"({attempt}/{MAX_ATTEMPTS}) [{shortcode}]", flush=True)

        return retry_on_rate_limit(
            lambda: self._raw_media_once(shortcode),
            proxied=self.proxied,
            on_retry=log_retry,
            before_retry=self.reset,
            stats=self.retry_stats,
        )

    def _raw_media_once(self, shortcode: str) -> Dict[str, Any]:
        """GraphQL 원본 응답에서 게시물 dict 를 꺼낸다 (재시도 없음)."""
        self.ensure_tokens()
        media_id = str(id_to_pk(shortcode))
        payload = {
            "lsd": self.tokens.lsd,
            "fb_api_caller_class": "RelayModern",
            "fb_api_req_friendly_name": self.config.friendly_name,
            "server_timestamps": "true",
            "variables": json.dumps({"media_id": media_id}, separators=(",", ":")),
            "doc_id": self.config.doc_id,
        }

        try:
            response = self._request_session().post(
                self.config.graphql_url,
                headers=self._headers(shortcode),
                data=payload,
                timeout=self.config.timeout,
            )
        except Exception as e:
            raise FetchError(f"GraphQL request failed: {type(e).__name__}: {e}")

        if is_login_redirect(response.url):
            raise RateLimitError("GraphQL request redirected to login - rate limit exceeded")
        if response.status_code == 429:
            raise RateLimitError("GraphQL returned HTTP 429")
        if response.status_code != 200:
            raise FetchError(f"GraphQL returned HTTP {response.status_code}")

        try:
            data = response.json()
        except Exception:
            raise FetchError("GraphQL returned a non-JSON response")

        # 레이트리밋은 HTTP 200 + errors 배열로 온다:
        #   {"errors":[{"message":"Rate limit exceeded","severity":"CRITICAL",
        #               "code":1675004}],"extensions":{"is_final":true}}
        errors = data.get("errors") or []
        for error in errors:
            if error.get("code") == RATE_LIMIT_ERROR_CODE or \
                    "rate limit" in str(error.get("message", "")).lower():
                with self._lock:
                    self.state.record_rate_limit()
                raise RateLimitError(
                    f"{error.get('message', 'Rate limit exceeded')} "
                    f"(code={error.get('code')}, after {self.state.requests} requests)")

        # GraphQL 은 data 와 errors 를 함께 주기도 한다 (차단/게이팅 게시물에서
        # 일부 필드만 실패). data 가 있으면 그대로 쓴다.
        media = (data.get("data") or {}).get("xig_polaris_media")
        if not media and errors:
            # data 도 없고 errors 만 있으면 서버 오류다. 삭제(MediaNotFound)로
            # 오분류하면 멀쩡한 게시물이 use_is=N 으로 내려가므로 구분한다.
            raise FetchError(
                f"[{shortcode}] GraphQL error: {str(errors[0].get('message', ''))[:120]}")
        if not media:
            with self._lock:
                streak = self.state.record_not_found()
                limited = self.state.looks_rate_limited
            if limited:
                # errors 없이 계속 빈 응답이면 형태가 바뀐 스로틀링일 수 있다
                raise RateLimitError(
                    f"{streak} consecutive empty media responses without an error payload "
                    f"(after {self.state.requests} requests) - likely throttled")
            raise MediaNotFoundError(
                f"[{shortcode}] post does not exist (removed or private account)")

        info = media.get("if_not_gated_logged_out")
        if not info:
            # 게이팅 응답도 API 가 살아있다는 증거다
            with self._lock:
                self.state.record_hit()
            raise MediaGatedError(shortcode, GatingRuling.from_dict(media.get("gating_ruling")))

        with self._lock:
            self.state.record_hit()
        return info

    def fetch(self, url: str) -> Media:
        """게시물 URL(또는 shortcode) -> Media."""
        shortcode = extract_shortcode(url)
        return parse_media(self.raw_media(shortcode), shortcode=shortcode)

    def gating_ruling(self, shortcode: str) -> GatingRuling:
        """게시물 접근이 막힌 사유를 조회한다 (진단용)."""
        self.ensure_tokens()
        response = self.session.get(
            self.config.ruling_url,
            headers={
                "X-IG-App-ID": self.config.app_id,
                "X-ASBD-ID": self.config.asbd_id,
                "X-IG-WWW-Claim": "0",
                "Origin": self.config.base_url,
                "Accept": "*/*",
            },
            params={"content_type": "MEDIA", "target_id": str(id_to_pk(shortcode))},
            timeout=self.config.timeout,
        )
        try:
            data = response.json()
        except Exception:
            return GatingRuling()
        return GatingRuling(
            gating_type=data.get("gating_type_thrift"),
            title=data.get("title") or "",
            description=data.get("description") or "",
        )


# --------------------------------------------------------------------------
# 파싱
# --------------------------------------------------------------------------

def _versions(item: dict) -> List[VideoVersion]:
    out = []
    for raw in item.get("video_versions") or []:
        version = VideoVersion.from_graphql(raw)
        if version:
            out.append(version)
    return out


def _thumbnails(item: dict) -> List[Thumbnail]:
    out = []
    for raw in (item.get("image_versions2") or {}).get("candidates") or []:
        thumb = Thumbnail.from_graphql(raw)
        if thumb:
            out.append(thumb)
    return out


def _parse_file(item: dict, index: int) -> Optional[File]:
    """게시물(또는 캐러셀 자식) 하나 -> File."""
    versions = _versions(item)
    thumbs = _thumbnails(item)
    is_video = item.get("media_type") == MEDIA_TYPE_VIDEO or bool(versions)

    if is_video and versions:
        url = versions[0].url
        file_type = FileType.VIDEO
    elif thumbs:
        url = thumbs[0].url
        file_type = FileType.IMAGE
    else:
        return None

    return File(
        type=file_type,
        index=index,
        url=url,
        accessibility_caption=item.get("accessibility_caption") or "",
        width=item.get("original_width"),
        height=item.get("original_height"),
        video_versions=versions,
        thumbnails=thumbs,
        dash_manifest=item.get("video_dash_manifest"),
        duration=item.get("video_duration"),
    )


def _caption_text(item: dict) -> str:
    caption = item.get("caption")
    if isinstance(caption, dict):
        return caption.get("text") or ""
    if isinstance(caption, str):
        return caption
    return ""


def _count(value) -> int:
    if isinstance(value, dict):
        value = value.get("count")
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        digits = re.sub(r"[^0-9]", "", value)
        return int(digits) if digits else 0
    return 0


def parse_media(item: dict, shortcode: str = "") -> Media:
    """GraphQL `if_not_gated_logged_out` dict -> Media."""
    media_type = item.get("media_type")
    files: List[File] = []

    if media_type == MEDIA_TYPE_CAROUSEL:
        kind = MediaType.CAROUSEL
        for index, child in enumerate(item.get("carousel_media") or []):
            file = _parse_file(child, index)
            if file:
                files.append(file)
    else:
        kind = MediaType.REEL if item.get("product_type") == "clips" else MediaType.FEED
        file = _parse_file(item, 0)
        if file:
            files.append(file)

    view_count = item.get("play_count")
    if view_count is None:
        view_count = item.get("ig_play_count")

    pk = str(item.get("pk") or item.get("id") or "")
    return Media(
        user=User.from_graphql(item.get("user")),
        pk=pk,
        shortcode=item.get("code") or shortcode or (pk_to_id(pk) if pk.isdigit() else ""),
        files=files,
        type=kind,
        caption=_caption_text(item),
        accessibility_caption=item.get("accessibility_caption") or "",
        like_count=_count(item.get("like_count")),
        comment_count=_count(item.get("comment_count")),
        view_count=view_count if isinstance(view_count, int) else None,
        taken_at=item.get("taken_at"),
        location=Location.from_graphql(item.get("location")),
        original_width=item.get("original_width"),
        original_height=item.get("original_height"),
        has_audio=item.get("has_audio"),
        product_type=item.get("product_type") or "",
    )
