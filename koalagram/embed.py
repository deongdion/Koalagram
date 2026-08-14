"""
koalagram.embed — 인스타 embed 엔드포인트 기반 미디어 조회
==========================================================

기존 `Koalagram.fetch()` 는 게시물 **본 페이지**(`/reel/<code>`)를 로그아웃
상태로 긁는다. 인스타는 익명 열람에 한도를 두고 있어서, 한도를 넘기면 게시물
대신 로그인 페이지를 **HTTP 200** 으로 돌려준다. 그러면 파싱이 실패하고
"Could not find media data" 만 남는다 — 차단인지 삭제된 게시물인지도 모른다.

embed 엔드포인트(`/reel/<code>/embed/captioned/`)는 **외부 사이트 임베드용**이라
로그인 벽을 타지 않는다. 본 페이지가 막힌 IP 에서도 그대로 응답한다(실측 확인).

응답 HTML 안의 `"contextJSON":"<이스케이프된 JSON>"` 에 우리가 필요한 게 다 있다:

    {"context": {"type": "GraphVideo", "shortcode": "...", "copyright_blocked": false},
     "gql_data": {"shortcode_media": {
        "__typename": "GraphVideo", "is_video": true,
        "video_url": "https://scontent-…/…", "display_url": "https://…jpg",
        "owner": {...}, "edge_media_to_caption": {...}, ...}}}

사용:
    from koalagram.embed import fetch_embed, EmbedError

    media = await fetch_embed("https://www.instagram.com/reel/DaaQL9OyXtI")
    video = next((f.url for f in media.files if f.type == FileType.VIDEO), None)

설계:
 - 본 페이지를 안 건드리므로 계정/쿠키가 필요 없다.
 - 반환 타입은 기존 `Media` 그대로 — 호출부를 안 바꿔도 된다.
 - 실패 사유를 `EmbedError.reason` 으로 구분해 준다. 차단인지 삭제인지
   구분되지 않으면 재시도·알림 판단을 못 하기 때문.
"""

from __future__ import annotations

import asyncio
import json
import re
from typing import Any, Optional

from curl_cffi.requests import AsyncSession

from .lib import File, FileType, Media, MediaType, User

# embed 는 이 두 형태를 쓴다. captioned 쪽이 캡션까지 준다.
_EMBED_FORMS = (
    "https://www.instagram.com/{kind}/{code}/embed/captioned/",
    "https://www.instagram.com/{kind}/{code}/embed/",
)

_SHORTCODE_RE = re.compile(r"/(?:reel|reels|p|tv)/([A-Za-z0-9_-]+)")

# 로그인 벽 판별용 URL 조각
LOGIN_WALL_MARK = "accounts/login"

_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class EmbedError(RuntimeError):
    """embed 조회 실패. reason 으로 사유를 구분한다.

    reason:
      "no_shortcode"  — URL 에서 shortcode 를 못 뽑음
      "http"          — 2xx 아님
      "login_wall"    — embed 마저 로그인으로 튕김 (= IP 차단 심각)
      "no_context"    — contextJSON 없음. classify_failure() 로 아래 둘로 좁힌다
      "private"       — 계정이 임베드/외부공유 차단 (익명으로는 영구 불가)
      "not_found"     — 삭제된 게시물
      "copyright"     — 저작권 차단된 게시물 (embed 전용 제한)
      "no_media"      — 미디어 항목이 비어 있음

    copyright 인 경우에도 contextJSON 에는 owner/caption/썸네일이 남아 있다.
    그중 **owner(유저명)** 를 실어 보내면 호출부가 유저명형 본 페이지
    (`/{owner}/reel/{shortcode}/`)로 넘어가 영상을 건질 수 있다.
    """

    def __init__(self, reason: str, message: str, *,
                 owner: Optional[str] = None,
                 shortcode: Optional[str] = None) -> None:
        super().__init__(message)
        self.reason = reason
        self.owner = owner
        self.shortcode = shortcode


def extract_shortcode(url: str) -> Optional[str]:
    """게시물 URL 에서 shortcode 추출. `/reel/`, `/reels/`, `/p/`, `/tv/` 지원."""
    m = _SHORTCODE_RE.search(url or "")
    return m.group(1) if m else None


def _extract_context(html: str) -> Optional[dict[str, Any]]:
    """embed HTML 의 contextJSON 을 꺼내 dict 로.

    `"contextJSON":"{\\"context\\":…}"` 형태라 **두 번** 파싱해야 한다.
    정규식으로 끝 따옴표를 찾으면 본문 속 이스케이프된 따옴표에서 잘리므로,
    이스케이프를 고려해 문자열 리터럴 끝을 직접 스캔한다.
    """
    key = '"contextJSON":"'
    i = html.find(key)
    if i < 0:
        return None
    start = i + len(key) - 1          # 여는 따옴표
    k = start + 1
    n = len(html)
    while k < n:
        ch = html[k]
        if ch == "\\":
            k += 2
            continue
        if ch == '"':
            break
        k += 1
    try:
        inner = json.loads(html[start:k + 1])   # 1차: 문자열 리터럴 → 원본 JSON 문자열
        return json.loads(inner)                # 2차: JSON 문자열 → dict
    except (json.JSONDecodeError, ValueError):
        return None


def _caption(media: dict[str, Any]) -> str:
    edges = ((media.get("edge_media_to_caption") or {}).get("edges") or [])
    for e in edges:
        text = ((e or {}).get("node") or {}).get("text")
        if text:
            return str(text)
    return ""


def _user(media: dict[str, Any]) -> User:
    o = media.get("owner") or {}
    return User(
        pk=str(o.get("id") or ""),
        name=str(o.get("username") or ""),
        nickname=str(o.get("full_name") or ""),
        profile_url=str(o.get("profile_pic_url") or ""),
    )


def _files(media: dict[str, Any]) -> list[File]:
    """shortcode_media → File 목록. 캐러셀(GraphSidecar)은 자식들을 펼친다."""
    def one(node: dict[str, Any], index: int) -> Optional[File]:
        cap = str(node.get("accessibility_caption") or "")
        if node.get("is_video"):
            url = node.get("video_url")
            if url:
                return File(type=FileType.VIDEO, index=index,
                            url=str(url), accessibility_caption=cap)
            return None
        url = node.get("display_url") or node.get("thumbnail_src")
        if url:
            return File(type=FileType.IMAGE, index=index,
                        url=str(url), accessibility_caption=cap)
        return None

    children = ((media.get("edge_sidecar_to_children") or {}).get("edges") or [])
    if children:
        out = []
        for i, e in enumerate(children):
            f = one((e or {}).get("node") or {}, i)
            if f:
                out.append(f)
        return out

    f = one(media, 0)
    return [f] if f else []


def _media_type(media: dict[str, Any], ctx_type: str) -> MediaType:
    if media.get("edge_sidecar_to_children"):
        return MediaType.CAROUSEL
    if media.get("product_type") == "clips" or ctx_type == "GraphVideo":
        return MediaType.REEL
    return MediaType.FEED


def parse_embed_html(html: str, *, final_url: str = "") -> Media:
    """embed HTML → Media. 실패 시 EmbedError."""
    if LOGIN_WALL_MARK in (final_url or ""):
        raise EmbedError("login_wall",
                         "embed 마저 로그인으로 튕김 — IP 차단이 심각한 상태")

    ctx = _extract_context(html)
    if not ctx:
        raise EmbedError("no_context",
                         "contextJSON 없음 — 삭제/비공개 게시물이거나 embed 구조 변경")

    context = ctx.get("context") or {}
    media = (ctx.get("gql_data") or {}).get("shortcode_media") or {}

    if context.get("copyright_blocked"):
        # video_url 만 제거되고 owner/caption/썸네일은 남아 있다. 유저명을 실어
        # 보내면 호출부가 유저명형 본 페이지로 넘어가 영상을 건질 수 있다.
        raise EmbedError(
            "copyright", "저작권 차단된 게시물 (embed 전용 제한)",
            owner=str((media.get("owner") or {}).get("username") or "") or None,
            shortcode=str(media.get("shortcode")
                          or context.get("shortcode") or "") or None,
        )

    if not media:
        raise EmbedError("no_context", "gql_data.shortcode_media 없음")

    files = _files(media)
    if not files:
        raise EmbedError("no_media", "미디어 항목 0건 (video_url/display_url 모두 없음)")

    return Media(
        user=_user(media),
        pk=str(media.get("id") or ""),
        files=files,
        type=_media_type(media, str(context.get("type") or "")),
        caption=_caption(media),
        accessibility_caption=str(media.get("accessibility_caption") or ""),
        _koalagram=None,
    )


async def fetch_embed(url: str, *, session: Optional[AsyncSession] = None,
                      timeout: int = 20) -> Media:
    """게시물 URL → Media (embed 경유).

    session 을 주면 재사용한다(연결 유지). 안 주면 호출마다 새로 연다.
    captioned 형태가 실패하면 기본 embed 형태로 한 번 더 시도한다.
    """
    code = extract_shortcode(url)
    if not code:
        raise EmbedError("no_shortcode", f"shortcode 를 못 찾음: {url}")

    kind = "p" if "/p/" in url else ("tv" if "/tv/" in url else "reel")
    own = session is None
    s = session or AsyncSession(impersonate="safari")
    last: Optional[EmbedError] = None
    try:
        for form in _EMBED_FORMS:
            target = form.format(kind=kind, code=code)
            try:
                r = await s.get(target, headers={"User-Agent": _USER_AGENT},
                                allow_redirects=True, timeout=timeout)
            except Exception as exc:
                last = EmbedError("http", f"요청 실패: {type(exc).__name__}: {exc}")
                continue
            if not (200 <= r.status_code < 300):
                last = EmbedError("http", f"HTTP {r.status_code}")
                continue
            try:
                return parse_embed_html(r.text or "",
                                        final_url=str(getattr(r, "url", "") or ""))
            except EmbedError as e:
                last = e
                if e.reason == "login_wall":
                    raise          # 차단은 다른 형태로 재시도해도 소용없다
        raise last or EmbedError("no_context", "embed 응답을 파싱하지 못함")
    finally:
        if own:
            try:
                await s.close()
            except Exception:
                pass


# ── 유저명형 본 페이지 (저작권 차단 건 우회) ────────────────────────
# `/{username}/reel/{code}/` 는 프로필 컨텍스트로 취급돼 익명 열람 제한을 다르게
# 건다. 실측: 유저명 없는 `/reel/{code}/` 가 로그인으로 튕기는 상태에서도 이
# 형태는 통과했고, embed 가 저작권으로 막던 건들의 영상까지 그대로 나왔다.
# (저작권 차단은 embed 전용 제한이라 본 페이지에는 적용되지 않는다)
#
# 데이터 위치가 embed 와 다르다. embed 는 contextJSON, 여기는 `data-content-len`
# 스크립트 블록 안의 `video_versions` 배열(해상도별). 인스타 영상 URL 은 확장자가
# 없어서(`/o1/v/t2/f2/m86/AQ…`) `.mp4` 로 찾으면 못 찾는다.

def _script_jsons(html: str):
    """`data-content-len="N"` 스크립트 블록들의 JSON 문자열을 순서대로 내놓는다."""
    pos = 0
    while True:
        start = html.find('data-content-len="', pos)
        if start == -1:
            return
        len_end = html.find('"', start + 18)
        json_start = html.find(">", len_end) + 1
        json_end = html.find("</script>", json_start)
        if json_end == -1:
            return
        yield html[json_start:json_end]
        pos = json_end


def _collect(obj: Any, key: str, out: list) -> None:
    """중첩 dict/list 를 훑어 key 에 해당하는 값을 모두 모은다."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k == key:
                out.append(v)
            _collect(v, key, out)
    elif isinstance(obj, list):
        for v in obj:
            _collect(v, key, out)


def parse_main_page(html: str, *, final_url: str = "") -> Media:
    """유저명형 본 페이지 HTML → Media. 영상 URL 은 video_versions 에서 뽑는다."""
    if LOGIN_WALL_MARK in (final_url or ""):
        raise EmbedError("login_wall", "유저명형 본 페이지도 로그인으로 튕김")

    versions: list = []
    owner_names: list = []
    for raw in _script_jsons(html):
        try:
            data = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            continue
        _collect(data, "video_versions", versions)
        _collect(data, "username", owner_names)

    urls: list[str] = []
    for vv in versions:
        if isinstance(vv, list):
            for v in vv:
                if isinstance(v, dict) and v.get("url"):
                    urls.append(str(v["url"]))
    if not urls:
        raise EmbedError("no_media", "video_versions 에서 URL 을 못 찾음")

    return Media(
        user=User(pk="", name=str(owner_names[0]) if owner_names else "",
                  nickname="", profile_url=""),
        pk="",
        files=[File(type=FileType.VIDEO, index=0, url=urls[0])],
        type=MediaType.REEL,
        caption="",
        accessibility_caption="",
        _koalagram=None,
    )


async def fetch_main_page(username: str, shortcode: str, *,
                          session: Optional[AsyncSession] = None,
                          kind: str = "reel", timeout: int = 30) -> Media:
    """유저명형 본 페이지에서 영상 URL 을 가져온다.

    embed 가 `copyright` 로 막은 건의 우회 경로. 응답이 950KB 가량으로 embed 보다
    무거우므로 **막힌 건에만** 쓴다.
    """
    if not username or not shortcode:
        raise EmbedError("no_shortcode", "유저명/shortcode 가 비어 있음")

    url = f"https://www.instagram.com/{username}/{kind}/{shortcode}/"
    own = session is None
    s = session or AsyncSession(impersonate="safari")
    try:
        try:
            r = await s.get(url, headers={"User-Agent": _USER_AGENT},
                            allow_redirects=True, timeout=timeout)
        except Exception as exc:
            raise EmbedError("http", f"요청 실패: {type(exc).__name__}: {exc}")
        if not (200 <= r.status_code < 300):
            raise EmbedError("http", f"HTTP {r.status_code}")
        return parse_main_page(r.text or "",
                               final_url=str(getattr(r, "url", "") or ""))
    finally:
        if own:
            try:
                await s.close()
            except Exception:
                pass


# ── 내부 oEmbed — 실패 사유 판별기 ──────────────────────────────────
# `https://www.instagram.com/api/v1/oembed/?url=…` + x-ig-app-id 헤더.
# 9KB 짜리 JSON 으로 author_name / media_id / 캡션 / 썸네일을 준다.
#
# 유저명을 얻는 용도로는 embed 보다 낫지 않다 — embed 는 정상 건에서 영상까지
# 한 번에 주고, 저작권 건에서도 owner 를 준다. oEmbed 의 진짜 값어치는
# **실패 사유를 정확히 구분**해 주는 것이다:
#
#   HTTP 200 → 정상 (author_name 있음)
#   HTTP 403 "Private media"  → 계정이 임베드/외부공유를 차단. 익명으로는 영구 불가
#   HTTP 404 "No Media Match" → 삭제된 게시물
#
# embed 의 `no_context` 는 이 둘을 구분 못 해서 "삭제/비공개" 로 뭉뚱그렸다.
# 구분되면 재시도 여부와 알림 문구를 정확히 정할 수 있다.

OEMBED_URL = "https://www.instagram.com/api/v1/oembed/"
IG_APP_ID = "936619743392459"      # 인스타 웹앱이 공개적으로 쓰는 값


async def fetch_oembed(url: str, *, session: Optional[AsyncSession] = None,
                       timeout: int = 20) -> dict[str, Any]:
    """게시물 URL → oEmbed 정보(dict). 실패 시 EmbedError.

    반환 키: author_name, author_url, author_id, media_id, title,
             thumbnail_url, html …
    """
    own = session is None
    s = session or AsyncSession(impersonate="chrome")
    try:
        try:
            r = await s.get(OEMBED_URL, params={"url": url},
                            headers={"User-Agent": _USER_AGENT,
                                     "x-ig-app-id": IG_APP_ID},
                            timeout=timeout)
        except Exception as exc:
            raise EmbedError("http", f"요청 실패: {type(exc).__name__}: {exc}")

        body = (r.text or "").strip()
        if r.status_code == 403:
            raise EmbedError("private",
                             "비공개/임베드 차단 계정 — 익명 조회 불가 "
                             f"({body[:40]})")
        if r.status_code == 404:
            raise EmbedError("not_found", f"삭제된 게시물 ({body[:40]})")
        if not (200 <= r.status_code < 300):
            raise EmbedError("http", f"HTTP {r.status_code}: {body[:60]}")
        try:
            data = r.json()
        except Exception:
            raise EmbedError("http", f"JSON 아님: {body[:60]}")
        if not isinstance(data, dict) or not data.get("author_name"):
            raise EmbedError("no_media", "oEmbed 에 author_name 없음")
        return data
    finally:
        if own:
            try:
                await s.close()
            except Exception:
                pass


async def classify_failure(url: str, *,
                           session: Optional[AsyncSession] = None) -> str:
    """embed 가 no_context 로 실패했을 때 진짜 사유를 판별한다.

    반환: "private" | "not_found" | "unknown"
    판별 자체가 실패하면 "unknown" — 원래 사유를 유지하라는 뜻.
    """
    try:
        await fetch_oembed(url, session=session)
    except EmbedError as e:
        return e.reason if e.reason in ("private", "not_found") else "unknown"
    except Exception:
        return "unknown"
    return "unknown"      # oEmbed 는 되는데 embed 만 실패 — 원인 불명


__all__ = ["fetch_embed", "fetch_main_page", "fetch_oembed", "classify_failure",
           "parse_embed_html", "parse_main_page", "extract_shortcode",
           "EmbedError"]
