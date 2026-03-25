LOCATIONS_PROMPT = """
Extract location names from this Instagram post. Return ONLY a JSON list of location names to search.
DO NOT search Google Maps - just extract the names mentioned in the caption and image descriptions.

Caption: {caption}
{additional}

STRICT RULES:
1. Extract ONLY specific place names that are actual locations (restaurants, cafes, universities, stores, neighborhoods, etc.)
2. SKIP generic category terms: 카페, 맛집, 레스토랑, 대학교, 공원, 호텔, 술집, 일식, 한식, 중식, 양식, etc.
3. For hashtags, remove the # symbol
4. If a place is mentioned with variations, choose the most complete/official name
5. Include foreign language place names as-is (Japanese, English, etc.)
6. Look for location clues in BOTH caption text AND image descriptions
7. Include neighborhood/district names if mentioned (e.g., 이태원, 강남, 홍대 etc.)

Return ONLY JSON array format (no other text, no explanation):
["place1", "place2"]

Examples:
- "#군산대 #국립군산대학교 #대학교" → ["국립군산대학교"]
- "#魚の旨い店 #江戸富士 #일식" → ["魚の旨い店", "江戸富士"]  
- "#카페 #스타벅스경복궁점 #커피" → ["스타벅스경복궁점"]
- "#서울맛집 #육회자매집 #육회" → ["육회자매집"]
- "#부산대 #부산대학교 #경성대" → ["부산대학교", "경성대"]
- "#カフェ #블루보틀청담" → ["블루보틀청담"]
- "오늘 스타벅스 강남점에서 커피 한잔" → ["스타벅스 강남점"]
- "떠그클럽이 이태원에 마트를 차렸다" → ["이태원"]
"""

SUMMARY_PROMPT = """
Caption: {caption}
{additional}

이 인스타그램 게시물을 분석하고 다음 내용을 포함하여 한국어로 요약하세요:
1. 주요 주제
2. 핵심 메시지
3. 언급된 장소/브랜드
4. 분위기/톤
5. 중요한 세부사항

반드시 한국어로 2-3개 문단으로 작성하세요. 모든 내용은 한국어로만 출력하세요.
"""