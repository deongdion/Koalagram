# 🐨 Koalagram

Instagram 미디어 분석 도구 - Groq AI를 사용하여 게시물을 분석하고 위치 정보를 추출합니다.

## ✨ 기능

- 📷 Instagram 게시물 정보 추출 (피드, 캐러셀, 릴스)
- 🧱 로그인 벽 우회 — embed 엔드포인트 기반 조회 (`koalagram.embed`)
- 🤖 Groq AI를 통한 자동 요약
- 📍 위치 정보 자동 추출 및 Google Maps 검색
- 💾 미디어 파일 다운로드
- 🚀 비동기 처리로 빠른 성능

## 📦 설치

```bash
pip install -r requirements.txt
```

## 🔧 설정

1. `.env` 파일을 생성하고 Groq API 키를 설정하세요:

```env
GROQ_API_KEY=your_groq_api_key_here
DEBUG=True
```

2. Groq API 키 받기:
   - [Groq Console](https://console.groq.com)에서 가입
   - API Keys 섹션에서 키 생성

## 🚀 사용법

### 기본 예제

```python
import asyncio
from koalagram import Koalagram

async def main():
    # Koalagram 인스턴스 생성
    client = Koalagram(groq_api_key="YOUR_API_KEY", debug=True)
    
    # Instagram URL
    url = "https://www.instagram.com/p/EXAMPLE/"
    
    # 미디어 정보 가져오기
    media = await client.fetch(url)
    
    # AI 분석 (요약 + 위치)
    result = await media.analyze()
    
    print(f"요약: {result.summary}")
    print(f"위치: {[loc.name for loc in result.locations]}")
    
    # 파일 다운로드
    await client.download(media)

asyncio.run(main())
```

### 로그인 벽에 막힐 때 — `koalagram.embed`

Instagram은 로그아웃 상태의 게시물 열람에 한도를 둡니다. 한도를 넘기면 게시물
페이지 대신 **로그인 페이지를 HTTP 200으로** 돌려주기 때문에, 본 페이지를 긁는
`Koalagram.fetch()`는 `Could not find media data`만 남기고 실패합니다.
차단인지 삭제된 게시물인지도 구분되지 않습니다.

`koalagram.embed`는 외부 임베드용 엔드포인트를 써서 이 벽을 타지 않습니다.

```python
from koalagram import fetch_embed, fetch_main_page, classify_failure, EmbedError, FileType

try:
    media = await fetch_embed("https://www.instagram.com/reel/XXXXXXXXXXX/")
    video = next((f.url for f in media.files if f.type == FileType.VIDEO), None)

except EmbedError as e:
    if e.reason == "copyright" and e.owner:
        # 음원이 들어간 릴스는 embed만 막힙니다. 유저명이 붙은 본 페이지
        # (/{owner}/reel/{code}/)는 저작권 제한도, 로그인 벽도 타지 않습니다.
        media = await fetch_main_page(e.owner, e.shortcode)
    else:
        # private(계정이 임베드 차단) / not_found(삭제) 를 정확히 구분
        print(await classify_failure(url))
```

| `EmbedError.reason` | 뜻 |
|---|---|
| `copyright` | 음원 저작권으로 embed만 차단. `owner`로 우회 가능 |
| `private` | 계정이 임베드/외부공유 차단. 익명 조회 불가 |
| `not_found` | 삭제된 게시물 |
| `login_wall` | embed마저 로그인으로 튕김 (IP 차단) |

`classify_failure()`는 내부 oEmbed(`/api/v1/oembed/`)로 `private`와
`not_found`를 가려냅니다. 확정적인 실패이므로 재시도할 필요가 없습니다.

### 커맨드라인 사용

```bash
python example.py
```

## 📊 응답 구조

### Media 객체
- `type`: 미디어 타입 (FEED, CAROUSEL, REEL)
- `user`: 사용자 정보
- `files`: 파일 목록 (이미지/비디오)
- `caption`: 게시물 캡션
- `accessibility_caption`: 접근성 설명

### AnalysisResult 객체
- `summary`: 한국어 요약
- `locations`: 발견된 위치 정보 목록

## 🛠️ 기술 스택

- **AI**: Groq API (llama-3.1-8b-instant)
- **Web Scraping**: curl-cffi
- **JSON Validation**: Pydantic
- **Async Support**: asyncio

## 📝 라이선스

MIT License

## 🤝 기여

Issues와 Pull Requests를 환영합니다!

## ⚠️ 주의사항

- Instagram의 이용약관을 준수하세요
- API 사용량 제한에 주의하세요
- 개인적인 용도로만 사용하세요