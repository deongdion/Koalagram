"""Koalagram - 인스타그램 게시물 스크래퍼 (GraphQL 기반, 로그인 불필요)."""

from .aio import AsyncGraphQLClient, AsyncKoalagram, FetchResult
from .analysis import AnalysisRequest, AnalysisResponse, analyze_media
from .errors import (
    FetchError,
    GatingRuling,
    InvalidURLError,
    KoalagramError,
    MediaGatedError,
    MediaNotFoundError,
    RateLimitError,
)
from .graphql import (
    GraphQLClient,
    GraphQLConfig,
    SessionTokens,
    extract_shortcode,
    id_to_pk,
    is_login_redirect,
    parse_media,
    pk_to_id,
)
from .lib import DownloadResult, DownloadSummary, Koalagram, KoalagramConfig
from .map import GoogleMap, Place
from .models import (
    AnalysisResult,
    Coordinates,
    File,
    FileType,
    Location,
    Media,
    MediaType,
    Thumbnail,
    User,
    VideoVersion,
)
from .prompt import LOCATIONS_PROMPT, SUMMARY_PROMPT
from .proxy import ProxyPool, load_proxies, parse_proxy
from .retry import DELAYS, MAX_ATTEMPTS, RetryStats, retry_on_rate_limit
from .viewcount import ViewCountFetcher, ViewCountState, parse_view_count

__version__ = "2.0.0"

__all__ = [
    # 클라이언트
    "Koalagram",
    "AsyncKoalagram",
    "AsyncGraphQLClient",
    "FetchResult",
    "KoalagramConfig",
    "GraphQLClient",
    "GraphQLConfig",
    "SessionTokens",
    # 모델
    "Media",
    "File",
    "FileType",
    "MediaType",
    "User",
    "Location",
    "Coordinates",
    "VideoVersion",
    "Thumbnail",
    "AnalysisResult",
    "DownloadResult",
    "DownloadSummary",
    "ViewCountFetcher",
    "ViewCountState",
    "parse_view_count",
    "ProxyPool",
    "load_proxies",
    "parse_proxy",
    "RetryStats",
    "retry_on_rate_limit",
    "MAX_ATTEMPTS",
    "DELAYS",
    "Place",
    # 분석
    "analyze_media",
    "AnalysisRequest",
    "AnalysisResponse",
    "GoogleMap",
    "LOCATIONS_PROMPT",
    "SUMMARY_PROMPT",
    # 유틸
    "extract_shortcode",
    "id_to_pk",
    "pk_to_id",
    "is_login_redirect",
    "parse_media",
    # 예외
    "KoalagramError",
    "InvalidURLError",
    "FetchError",
    "MediaNotFoundError",
    "MediaGatedError",
    "GatingRuling",
    "RateLimitError",
]
