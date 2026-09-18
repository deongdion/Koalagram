# 🐨 Koalagram

로그인 없이 Instagram 게시물을 가져오는 스크래퍼. 선택적으로 Groq AI 요약과 장소 추출을 붙일 수 있습니다.

## ✨ 기능

- 📷 게시물 정보 추출 (피드 / 캐러셀 / 릴스)
- 🔓 **로그인 불필요** — 로그아웃 전용 GraphQL 엔드포인트 사용
- 🎬 **저작권 차단 게시물도 영상 URL 확보** — embed 표면이 막는 것도 가져옵니다
- ⚡ **asyncio 동시 조회** + 프록시 풀 라운드로빈
- 👁️ 조회수 보강 (embed 표면, 선택)
- 🤖 Groq AI 요약 · 📍 Google Maps 장소 검색 (선택)
- 💾 미디어 다운로드

## 📦 설치

```bash
pip install -r requirements.txt
```

## 🚀 사용법

### 동기

```python
from koalagram import Koalagram

client = Koalagram()
media = client.fetch_sync("https://www.instagram.com/reel/XXXXXXXXXXX/")

print(media.user.name, media.user.nickname)
print(media.like_count, media.comment_count)
print(media.best_video_url())     # 최고 화질 H.264 MP4

media.view()                      # 조회수는 필요할 때만 (embed 요청 1회)
print(media.view_count)
```

### 비동기 (대량 처리)

```python
import asyncio
from koalagram import AsyncKoalagram

async def main():
    async with AsyncKoalagram(proxy="proxies.txt") as client:
        results = await client.fetch_many(urls, concurrency=100)
        for r in results:
            if r.ok:
                print(r.media.shortcode, r.media.best_video_url())
            else:
                print(r.url, type(r.error).__name__)

asyncio.run(main())
```

### 프록시

`Koalagram(proxy=...)` 은 네 가지를 받습니다.

```python
Koalagram(proxy="proxies.txt")                 # 파일 (한 줄에 하나)
Koalagram(proxy="host:port:user:pass")         # 문자열 하나
Koalagram(proxy=["1.1.1.1:8080", "2.2.2.2:8080"])
Koalagram(proxy=None)                          # 직접 연결
```

표기: `host:port:user:pass`, `user:pass@host:port`, `http://user:pass@host:port`, `host:port`.
여러 개면 라운드로빈으로 씁니다.

### AI 분석 (선택)

```python
client = Koalagram()               # GROQ_API_KEY 환경변수 자동 인식
result = await media.analyze()
print(result.summary)
for loc in result.locations:
    print(loc.name, loc.address)
```

## 🔧 설정

`.env.example` 을 `.env` 로 복사해서 채웁니다.

```
GROQ_API_KEY=your_groq_api_key_here   # AI 분석을 쓸 때만 필요
DEBUG=False
```

## 📐 구조

| 모듈 | 역할 |
|---|---|
| `graphql.py` | 로그아웃 GraphQL 클라이언트, shortcode ↔ pk 변환, 응답 파싱 |
| `aio.py` | asyncio 판 (`AsyncKoalagram`, `AsyncGraphQLClient`) |
| `models.py` | 데이터 모델 (`Media`, `File`, `User`, `VideoVersion` …) |
| `viewcount.py` | embed 표면에서 조회수만 가져오기 |
| `proxy.py` | 프록시 풀 (파싱 · 라운드로빈) |
| `retry.py` | 레이트리밋 재시도 |
| `errors.py` | 예외 계층 |
| `analysis.py` | Groq 요약 · 장소 추출 (선택) |

### 예외

| 예외 | 의미 |
|---|---|
| `MediaNotFoundError` | 삭제됐거나 비공개 계정 |
| `MediaGatedError` | 연령 제한 등 — 게시물은 살아있음 (`.ruling` 에 사유) |
| `RateLimitError` | 익명 조회 한도 초과 |
| `FetchError` | 네트워크 / 일시적 서버 오류 |
| `InvalidURLError` | URL 파싱 실패 |

## ⚠️ 알아둘 점

- **조회수는 GraphQL 에 없습니다.** embed 표면에만 있어서 `view()` 를 따로 불러야 하고,
  embed 는 한도가 빡빡해 실패하면 조용히 `None` 을 돌려줍니다 (영상 URL 등 나머지는 그대로 유지).
- **영상 URL 은 약 36시간, 이미지는 약 108시간 뒤 만료됩니다** (URL 의 `oe` 파라미터가 만료 시각).
  장기 보관하려면 주기적으로 다시 받아야 합니다.
- **익명 조회에는 한도가 있습니다.** GraphQL 은 `errors[].code == 1675004` 로,
  embed 는 `/accounts/login` 리다이렉트로 알려줍니다. 프록시 풀을 쓰면 우회됩니다.
- 추출 방식은 [yt-dlp](https://github.com/yt-dlp/yt-dlp) 의 Instagram extractor 를 참고했습니다.

## 📄 라이선스

개인 학습용 프로젝트입니다.
