"""Koalagram - 인스타그램 게시물 스크래퍼 (GraphQL 기반, 로그인 불필요).

    from koalagram import Koalagram

    client = Koalagram()
    media = client.fetch_sync("https://www.instagram.com/reel/XXXXXXXXXXX/")
    print(media.user.name, media.like_count, media.best_video_url())
"""

import asyncio
import os
from dataclasses import dataclass, field, replace
from typing import Any, List, Optional

from .analysis import analyze_media, resolve_groq_api_key
from .errors import (
    FetchError,
    InvalidURLError,
    KoalagramError,
    MediaGatedError,
    MediaNotFoundError,
    RateLimitError,
)
from .graphql import GraphQLClient, GraphQLConfig, extract_shortcode, id_to_pk, pk_to_id
from .models import AnalysisResult, File, FileType, Media, MediaType
from .proxy import ProxyPool
from .retry import RetryStats
from .viewcount import ViewCountFetcher, ViewCountState


@dataclass
class DownloadResult:
    """다운로드 한 건의 결과."""
    file: File
    path: str = ""
    ok: bool = False
    error: str = ""
    size: int = 0


@dataclass
class DownloadSummary:
    """download() 전체 결과."""
    media_pk: str = ""
    folder: str = ""
    results: List[DownloadResult] = field(default_factory=list)

    @property
    def paths(self) -> List[str]:
        return [r.path for r in self.results if r.ok]

    @property
    def succeeded(self) -> int:
        return sum(1 for r in self.results if r.ok)

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if not r.ok)

    def __len__(self) -> int:
        return self.succeeded

    def __iter__(self):
        return iter(self.paths)


@dataclass
class KoalagramConfig:
    """클라이언트 동작 설정."""
    # 생략하면 GROQ_API_KEY 환경변수를 쓴다
    groq_api_key: Optional[str] = None
    debug: bool = False
    download_folder: str = "downloads"
    # 프록시: 문자열 하나 / proxies.txt 경로 / 목록
    proxy: Any = None
    graphql: GraphQLConfig = field(default_factory=GraphQLConfig)
    # view() 가 재시도를 전부 소진하면 이후로는 아예 시도하지 않는다
    disable_view_count_after_failure: bool = True


class Koalagram:
    """인스타그램 게시물을 가져오고 내려받는 클라이언트.

    모든 조회는 로그아웃 GraphQL 표면을 쓴다. 저작권 차단 게시물의
    영상 URL 도 받을 수 있고, 이미지/캐러셀/릴스를 한 경로로 처리한다.

    조회수는 이 표면에 없어서 fetch 결과의 view_count 는 None 이다.
    필요하면 `client.view(media)` 또는 `media.view()` 를 따로 부른다.
    """

    def __init__(self, groq_api_key: Optional[str] = None, debug: bool = False,
                 proxy: Any = None,
                 config: Optional[KoalagramConfig] = None):
        self.config = config or KoalagramConfig(groq_api_key=groq_api_key, debug=debug)
        if groq_api_key and not self.config.groq_api_key:
            self.config.groq_api_key = groq_api_key
        if not self.config.groq_api_key:
            self.config.groq_api_key = resolve_groq_api_key()
        if debug:
            self.config.debug = True
        if proxy:
            self.config.proxy = proxy
        # 프록시는 GraphQLConfig(frozen)에 복사해 둔다
        if self.config.proxy and not self.config.graphql.proxy:
            self.config.graphql = replace(self.config.graphql, proxy=self.config.proxy)

        self.client = GraphQLClient(config=self.config.graphql, debug=self.config.debug)
        self.view_counts = ViewCountFetcher(
            impersonate=self.config.graphql.impersonate,
            proxy=self.config.proxy,
            disable_on_failure=self.config.disable_view_count_after_failure,
        )

    # ---- 설정 단축 접근 ----

    @property
    def groq_api_key(self) -> Optional[str]:
        return self.config.groq_api_key

    @property
    def debug(self) -> bool:
        return self.config.debug

    @property
    def proxy(self):
        return self.config.proxy

    @property
    def proxies(self) -> "ProxyPool":
        return self.client.proxies

    @property
    def session(self):
        return self.client.session

    # ---- 조회 ----

    async def fetch(self, url: str) -> Media:
        """게시물 정보를 비동기로 가져온다."""
        return await asyncio.to_thread(self.fetch_sync, url)

    def fetch_sync(self, url: str) -> Media:
        """게시물 정보를 동기로 가져온다. 조회수가 필요하면 view() 를 부른다."""
        media = self.client.fetch(url)
        media._client = self
        return media

    # ---- 조회수 ----

    def view(self, target) -> Optional[int]:
        """조회수를 가져온다 (embed 표면).

        target 은 Media, 게시물 URL, shortcode 중 아무거나.
        Media 를 주면 media.view_count 에 채워 넣고 그 값을 돌려준다.

        GraphQL 은 조회수를 주지 않아 embed 를 써야 하는데, embed 는 한도가
        빡빡하다. 이미 값이 있거나 영상이 아니면 요청하지 않고, 한 번 막히면
        이후 호출은 요청 없이 None 을 돌려준다
        (KoalagramConfig.disable_view_count_after_failure).
        """
        media = target if isinstance(target, Media) else None
        if media is not None:
            if media.view_count is not None:
                return media.view_count
            if not media.is_video:
                return None
            shortcode = media.shortcode
        else:
            shortcode = extract_shortcode(str(target))

        if not shortcode:
            return None

        view_count = self.view_counts.fetch(shortcode, debug=self.debug)
        if media is not None and view_count is not None:
            media.view_count = view_count
        return view_count

    def fetch_many_sync(self, urls: List[str]) -> List[Media]:
        """여러 건을 순차 조회한다. 실패한 건은 건너뛴다."""
        out = []
        for url in urls:
            try:
                out.append(self.fetch_sync(url))
            except KoalagramError as e:
                if self.debug:
                    print(f"[koalagram] {url}: {type(e).__name__}: {e}")
        return out

    def reset_session(self, retry_view_counts: bool = False) -> None:
        """레이트리밋 이후 세션/토큰을 새로 발급한다.

        retry_view_counts=True 면 조회수 보강도 다시 활성화한다.
        """
        self.client.reset()
        if retry_view_counts:
            self.view_counts.state.reset()
            self.view_counts.session = None
            self.view_counts.__post_init__()

    # ---- 분석 ----

    async def analyze(self, media: Media) -> AnalysisResult:
        return await analyze_media(media, self)

    # ---- 다운로드 ----

    async def download(self, media: Media, folder: Optional[str] = None) -> DownloadSummary:
        """게시물의 모든 파일을 병렬로 내려받는다."""
        base = folder or self.config.download_folder
        media_folder = os.path.join(base, f"{media.user.name}_{media.pk}")
        os.makedirs(media_folder, exist_ok=True)

        tasks = []
        for file in media.files:
            filename = f"{file.index + 1:02d}{file.extension}"
            path = os.path.join(media_folder, filename)
            url = file.best_video_url() if file.is_video else file.url
            tasks.append(self._download_file(file, url or file.url, path))

        results = await asyncio.gather(*tasks, return_exceptions=True)

        summary = DownloadSummary(media_pk=media.pk, folder=media_folder)
        for file, result in zip(media.files, results):
            if isinstance(result, DownloadResult):
                summary.results.append(result)
            else:
                summary.results.append(DownloadResult(
                    file=file, ok=False, error=f"{type(result).__name__}: {result}"))
        if self.debug:
            print(f"[koalagram] downloaded {summary.succeeded}/{len(summary.results)} "
                  f"-> {media_folder}")
        return summary

    def download_sync(self, media: Media, folder: Optional[str] = None) -> DownloadSummary:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.download(media, folder))
        finally:
            loop.close()

    async def _download_file(self, file: File, url: str, path: str) -> DownloadResult:
        try:
            response = await asyncio.to_thread(self.client.session.get, url)
        except Exception as e:
            return DownloadResult(file=file, path=path, ok=False,
                                  error=f"{type(e).__name__}: {e}")
        if response.status_code != 200:
            return DownloadResult(file=file, path=path, ok=False,
                                  error=f"HTTP {response.status_code}")
        with open(path, "wb") as f:
            f.write(response.content)
        return DownloadResult(file=file, path=path, ok=True, size=len(response.content))


__all__ = [
    "Koalagram", "KoalagramConfig", "DownloadResult", "DownloadSummary",
    "ViewCountFetcher", "ViewCountState", "RetryStats",
    "Media", "File", "FileType", "MediaType", "AnalysisResult",
    "GraphQLClient", "GraphQLConfig",
    "extract_shortcode", "id_to_pk", "pk_to_id",
    "KoalagramError", "InvalidURLError", "FetchError",
    "MediaNotFoundError", "MediaGatedError", "RateLimitError",
]
