# 🐨 Koalagram

Instagram 미디어 분석 도구 - Groq AI를 사용하여 게시물을 분석하고 위치 정보를 추출합니다.

## ✨ 기능

- 📷 Instagram 게시물 정보 추출 (피드, 캐러셀, 릴스)
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