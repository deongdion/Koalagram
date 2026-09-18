"""Koalagram 데이터 모델 (전부 dataclass)."""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

# Instagram media_type 코드
MEDIA_TYPE_IMAGE = 1
MEDIA_TYPE_VIDEO = 2
MEDIA_TYPE_CAROUSEL = 8


class FileType(Enum):
    IMAGE = "IMAGE"
    VIDEO = "VIDEO"


class MediaType(Enum):
    FEED = "FEED"          # 단일 이미지 / 영상
    CAROUSEL = "CAROUSEL"  # 여러 장
    REEL = "REEL"          # 릴스 (product_type == clips)


@dataclass
class Coordinates:
    lat: float
    lng: float

    def __str__(self) -> str:
        return f"{self.lat},{self.lng}"


@dataclass
class Location:
    """장소 정보. 게시물에 태그된 위치 또는 분석으로 찾아낸 장소."""
    name: str
    address: str = ""
    rating: float = 0.0
    review_count: int = 0
    category: str = ""
    coordinates: Optional[Coordinates] = None
    pk: str = ""

    @classmethod
    def from_graphql(cls, data: Optional[dict]) -> Optional["Location"]:
        if not data:
            return None
        lat, lng = data.get("lat"), data.get("lng")
        coords = Coordinates(float(lat), float(lng)) if lat is not None and lng is not None else None
        return cls(
            name=data.get("name") or "",
            address=data.get("address") or "",
            coordinates=coords,
            pk=str(data.get("pk") or ""),
        )


@dataclass
class User:
    pk: str = ""
    name: str = ""       # username
    nickname: str = ""   # full_name
    profile_url: str = ""
    is_verified: bool = False

    @property
    def profile_link(self) -> str:
        return f"https://www.instagram.com/{self.name}/" if self.name else ""

    @classmethod
    def from_graphql(cls, data: Optional[dict]) -> "User":
        data = data or {}
        return cls(
            pk=str(data.get("pk") or data.get("id") or ""),
            name=data.get("username") or "",
            nickname=data.get("full_name") or "",
            profile_url=data.get("profile_pic_url") or "",
            is_verified=bool(data.get("is_verified")),
        )


@dataclass
class VideoVersion:
    """한 영상의 화질별 후보.

    Instagram 은 progressive MP4 를 여러 type 으로 내려준다
    (101 > 102 > 103 순으로 품질이 높다). 전부 H.264/AAC 다.
    """
    url: str
    type_id: str = ""
    width: Optional[int] = None
    height: Optional[int] = None

    @classmethod
    def from_graphql(cls, data: dict) -> Optional["VideoVersion"]:
        url = data.get("url")
        if not url:
            return None
        return cls(
            url=url,
            type_id=str(data.get("type") or data.get("id") or ""),
            width=_int_or_none(data.get("width")),
            height=_int_or_none(data.get("height")),
        )


@dataclass
class Thumbnail:
    url: str
    width: Optional[int] = None
    height: Optional[int] = None

    @classmethod
    def from_graphql(cls, data: dict) -> Optional["Thumbnail"]:
        url = data.get("url")
        if not url:
            return None
        return cls(url=url,
                   width=_int_or_none(data.get("width")),
                   height=_int_or_none(data.get("height")))


@dataclass
class File:
    """게시물에 포함된 미디어 하나 (캐러셀이면 자식 하나)."""
    type: FileType
    index: int
    url: str
    accessibility_caption: str = ""
    width: Optional[int] = None
    height: Optional[int] = None
    video_versions: List[VideoVersion] = field(default_factory=list)
    thumbnails: List[Thumbnail] = field(default_factory=list)
    dash_manifest: Optional[str] = None
    duration: Optional[float] = None

    @property
    def is_video(self) -> bool:
        return self.type is FileType.VIDEO

    @property
    def extension(self) -> str:
        return ".mp4" if self.is_video else ".jpg"

    def best_video_url(self, preferred_types=("101", "102", "103")) -> Optional[str]:
        """가장 좋은 화질의 H.264 영상 URL."""
        by_type = {v.type_id: v.url for v in self.video_versions if v.url}
        for type_id in preferred_types:
            if type_id in by_type:
                return by_type[type_id]
        if self.video_versions:
            return self.video_versions[0].url
        return self.url if self.is_video else None


@dataclass
class AnalysisResult:
    """AI 분석 결과 (요약 + 장소)."""
    summary: Optional[str] = None
    locations: List[Location] = field(default_factory=list)


@dataclass
class Media:
    """인스타그램 게시물 하나."""
    user: User = field(default_factory=User)
    pk: str = ""
    shortcode: str = ""
    files: List[File] = field(default_factory=list)
    type: MediaType = MediaType.FEED
    caption: str = ""
    accessibility_caption: str = ""
    like_count: int = 0
    comment_count: int = 0
    view_count: Optional[int] = None
    taken_at: Optional[int] = None          # unix timestamp
    location: Optional[Location] = None
    original_width: Optional[int] = None
    original_height: Optional[int] = None
    has_audio: Optional[bool] = None
    product_type: str = ""
    _client: Optional[Any] = field(default=None, repr=False, compare=False)

    # ---- 편의 속성 ----

    @property
    def url(self) -> str:
        return f"https://www.instagram.com/p/{self.shortcode}/" if self.shortcode else ""

    @property
    def video_files(self) -> List[File]:
        return [f for f in self.files if f.is_video]

    @property
    def image_files(self) -> List[File]:
        return [f for f in self.files if not f.is_video]

    @property
    def video_urls(self) -> List[str]:
        return [f.url for f in self.video_files]

    @property
    def image_urls(self) -> List[str]:
        return [f.url for f in self.image_files]

    @property
    def is_video(self) -> bool:
        return bool(self.video_files)

    @property
    def taken_at_dt(self) -> Optional[datetime]:
        if self.taken_at is None:
            return None
        return datetime.fromtimestamp(self.taken_at, tz=timezone.utc)

    def best_video_url(self) -> Optional[str]:
        """대표 영상의 최고 화질 URL."""
        for file in self.video_files:
            url = file.best_video_url()
            if url:
                return url
        return None

    def to_dict(self) -> Dict[str, Any]:
        """JSON 직렬화용 dict (내부 클라이언트 참조는 제외)."""
        def encode(value):
            if isinstance(value, Enum):
                return value.value
            return value

        data = asdict(self, dict_factory=lambda items: {
            k: encode(v) for k, v in items if k != "_client"
        })
        return data

    def view(self) -> Optional[int]:
        """조회수를 가져와 view_count 에 채운다 (embed 표면, 1회).

        GraphQL 응답에는 조회수가 없어서 별도 요청이 필요하다.
        클라이언트 없이 만들어진 Media 면 None.
        """
        if self.view_count is not None:
            return self.view_count
        if self._client is None:
            return None
        return self._client.view(self)

    # ---- 분석 (선택 기능) ----

    async def analyze(self) -> AnalysisResult:
        """캡션에서 요약과 장소를 뽑는다. Groq API 키가 있어야 동작한다."""
        if self._client is None:
            return AnalysisResult()
        from .analysis import analyze_media
        return await analyze_media(self, self._client)


def _int_or_none(value) -> Optional[int]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None
