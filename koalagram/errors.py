"""Koalagram 예외 계층."""

from dataclasses import dataclass
from typing import Optional


class KoalagramError(Exception):
    """모든 Koalagram 예외의 최상위."""


class InvalidURLError(KoalagramError, ValueError):
    """인스타그램 게시물 URL / shortcode 로 해석할 수 없음."""


class FetchError(KoalagramError):
    """네트워크 요청 자체가 실패."""


class MediaNotFoundError(KoalagramError):
    """게시물이 존재하지 않음 (삭제됐거나 비공개 계정).

    GraphQL 응답의 `xig_polaris_media` 가 null 인 경우.
    """


@dataclass
class GatingRuling:
    """비로그인 사용자에게 게시물을 막은 사유."""
    gating_type: Optional[int] = None
    title: str = ""
    description: str = ""

    @property
    def is_age_restricted(self) -> bool:
        return self.gating_type == 3

    @classmethod
    def from_dict(cls, data: Optional[dict]) -> "GatingRuling":
        data = data or {}
        return cls(
            gating_type=data.get("gating_type"),
            title=data.get("title") or "",
            description=data.get("description") or "",
        )


class MediaGatedError(KoalagramError):
    """게시물은 존재하지만 비로그인 상태로는 볼 수 없음.

    대부분 연령 제한(gating_type=3)이다. 게시물 자체는 살아있으므로
    삭제(MediaNotFoundError)와 구분해서 다뤄야 한다.
    """

    def __init__(self, shortcode: str, ruling: Optional[GatingRuling] = None):
        self.shortcode = shortcode
        self.ruling = ruling or GatingRuling()
        message = self.ruling.title or "Content is not available to logged-out viewers"
        if self.ruling.description:
            message = f"{message}: {self.ruling.description}"
        super().__init__(f"[{shortcode}] {message}")


class RateLimitError(KoalagramError):
    """익명 조회 한도 초과. 인스타그램이 로그인 페이지로 리다이렉트한 상태."""
