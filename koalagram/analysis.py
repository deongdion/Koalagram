"""캡션 기반 AI 분석 (요약 + 장소 추출).

Groq API 를 쓰는 선택 기능이다. `groq` 패키지가 없거나 API 키가 없으면
빈 결과를 돌려주고, 나머지 스크래핑 기능은 그대로 동작한다.
"""

import asyncio
import json
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, List, Optional

from .models import AnalysisResult, Coordinates, Location, Media

if TYPE_CHECKING:  # pragma: no cover
    from .lib import Koalagram

GROQ_MODEL = "llama-3.1-8b-instant"
GROQ_ENV_VAR = "GROQ_API_KEY"


def resolve_groq_api_key(explicit: Optional[str] = None) -> Optional[str]:
    """API 키를 정한다: 인자 -> GROQ_API_KEY 환경변수 -> None.

    python-dotenv 가 설치돼 있으면 .env 도 읽는다.
    """
    if explicit:
        return explicit
    key = os.getenv(GROQ_ENV_VAR)
    if key:
        return key
    try:
        from dotenv import load_dotenv
    except ImportError:
        return None
    load_dotenv()
    return os.getenv(GROQ_ENV_VAR)

_SYSTEM_PROMPT = (
    "You are a JSON-only response bot. You MUST:\n"
    "1. Return ONLY valid JSON\n"
    "2. Start with { and end with }\n"
    "3. Use double quotes for all strings\n"
    "4. Ensure all strings are properly terminated\n"
    "5. Follow the exact schema provided\n"
    "6. Never include explanations or additional text"
)

_SCHEMA = {
    "type": "object",
    "properties": {
        "locations": {"type": "array", "items": {"type": "string"},
                      "description": "List of location names found in the post"},
        "summary": {"type": "string", "description": "Korean language summary of the post"},
    },
    "required": ["locations", "summary"],
}

_EXAMPLE = {
    "locations": ["이태원", "스타벅스 강남점"],
    "summary": "떠그클럽이 이태원에 새로운 매장을 오픈했습니다. 아디다스와의 협업 제품을 선보이며 많은 관심을 받고 있습니다.",
}


@dataclass
class AnalysisRequest:
    """모델에 보낼 입력을 담는다."""
    caption: str
    additional_info: str = ""
    model: str = GROQ_MODEL
    temperature: float = 0.1
    max_tokens: int = 1024

    def build_prompt(self) -> str:
        from .prompt import LOCATIONS_PROMPT, SUMMARY_PROMPT
        locations_prompt = LOCATIONS_PROMPT.format(
            caption=self.caption, additional=self.additional_info)
        summary_prompt = SUMMARY_PROMPT.format(
            caption=self.caption, additional=self.additional_info)
        return f"""You are analyzing an Instagram post. Please perform TWO tasks:

1. Extract location names from the caption and image descriptions
2. Create a summary of the post

=== TASK 1: EXTRACT LOCATIONS ===
{locations_prompt}

=== TASK 2: CREATE SUMMARY ===
{summary_prompt}

You MUST return a valid JSON object with this EXACT schema:
{json.dumps(_SCHEMA, ensure_ascii=False, indent=2)}

Example response:
{json.dumps(_EXAMPLE, ensure_ascii=False, indent=2)}

RULES:
1. Return ONLY the JSON object, no additional text
2. Ensure all strings are properly terminated with quotes
3. Use double quotes for JSON strings
4. Keep summary under 500 characters
"""


@dataclass
class AnalysisResponse:
    """모델 응답을 느슨하게 파싱한 결과."""
    locations: List[str] = field(default_factory=list)
    summary: str = ""

    @classmethod
    def parse(cls, text: str) -> "AnalysisResponse":
        """코드펜스/잡텍스트/깨진 JSON 까지 최대한 복구해서 읽는다."""
        raw = (text or "").strip()
        if not raw:
            return cls()

        if "```json" in raw:
            start = raw.find("```json") + 7
            end = raw.find("```", start)
            if end != -1:
                raw = raw[start:end].strip()
        elif "{" in raw:
            start, end = raw.find("{"), raw.rfind("}") + 1
            if end > start:
                raw = raw[start:end]

        data = None
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:
                import json_repair
                data = json.loads(json_repair.repair_json(raw))
            except Exception:
                return cls()

        if not isinstance(data, dict):
            return cls()
        locations = data.get("locations")
        return cls(
            locations=[str(x) for x in locations] if isinstance(locations, list) else [],
            summary=str(data.get("summary") or ""),
        )


def build_additional_info(media: Media, debug: bool = False) -> str:
    """접근성 캡션들을 프롬프트 보조 정보로 정리한다."""
    parts = []
    if media.accessibility_caption:
        parts.append(f"\nMain accessibility caption: {media.accessibility_caption}")
        if debug:
            print(f"📸 메인 접근성 캡션: {media.accessibility_caption}")

    file_captions = []
    for file in media.files:
        if file.accessibility_caption:
            file_captions.append(f"Image {file.index + 1}: {file.accessibility_caption}")
            if debug:
                print(f"  📷 이미지 {file.index + 1} 접근성 캡션: {file.accessibility_caption}")

    if file_captions:
        parts.append("\n\nIndividual image accessibility captions:\n" + "\n".join(file_captions))
    return "".join(parts)


async def _ask_groq(request: AnalysisRequest, api_key: str, debug: bool = False) -> AnalysisResponse:
    try:
        from groq import Groq
    except ImportError:
        if debug:
            print("[koalagram] groq 패키지가 설치되어 있지 않습니다 - 분석을 건너뜁니다")
        return AnalysisResponse()

    client = Groq(api_key=api_key)
    prompt = request.build_prompt()
    if debug:
        print(f"Using Groq model: {request.model} (prompt {len(prompt)} chars)")

    response = await asyncio.to_thread(
        client.chat.completions.create,
        model=request.model,
        messages=[{"role": "system", "content": _SYSTEM_PROMPT},
                  {"role": "user", "content": prompt}],
        temperature=request.temperature,
        max_tokens=request.max_tokens,
    )
    if not response.choices or not response.choices[0].message:
        if debug:
            print("No response from Groq API")
        return AnalysisResponse()

    text = response.choices[0].message.content or ""
    if debug:
        print(f"Raw Groq response: {text}")
    return AnalysisResponse.parse(text)


async def _resolve_locations(names: List[str], debug: bool = False) -> List[Location]:
    """장소 이름들을 구글맵으로 조회해 Location 목록으로."""
    if not names:
        return []
    from .map import GoogleMap

    google_map = GoogleMap(debug=debug)
    results = await asyncio.gather(*[google_map.search(name) for name in names],
                                   return_exceptions=True)

    locations: List[Location] = []
    seen = set()
    for name, result in zip(names, results):
        if isinstance(result, BaseException):
            if debug:
                print(f"Error searching for '{name}': {result}")
            continue
        place = result
        if not place or not getattr(place, "address", None) or place.address in seen:
            continue
        seen.add(place.address)

        coords: Optional[Coordinates] = None
        if getattr(place, "coordinates", None):
            try:
                lat, lng = str(place.coordinates).split(",")
                coords = Coordinates(lat=float(lat), lng=float(lng))
            except (ValueError, TypeError):
                coords = None

        locations.append(Location(
            name=place.name,
            address=place.address,
            category=getattr(place, "category", "") or "",
            coordinates=coords,
        ))
    return locations


async def analyze_media(media: Media, client: "Koalagram") -> AnalysisResult:
    """게시물 캡션에서 요약과 장소를 뽑는다."""
    if not media.caption or not getattr(client, "groq_api_key", None):
        return AnalysisResult()

    debug = bool(getattr(client, "debug", False))
    request = AnalysisRequest(
        caption=media.caption,
        additional_info=build_additional_info(media, debug=debug),
    )

    try:
        parsed = await _ask_groq(request, client.groq_api_key, debug=debug)
        locations = await _resolve_locations(parsed.locations, debug=debug)
    except Exception as e:
        if debug:
            print(f"Error in combined analysis: {type(e).__name__}: {e}")
            import traceback
            traceback.print_exc()
        return AnalysisResult()

    return AnalysisResult(summary=parsed.summary or None, locations=locations)
