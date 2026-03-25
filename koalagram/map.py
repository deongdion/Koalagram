import requests
import json
import re
from urllib.parse import quote, urlencode, unquote
from typing import List, Optional
import time
from dataclasses import dataclass
from datetime import datetime
import asyncio


@dataclass
class Place:
    name: str = ""
    address: str = ""
    category: str = ""
    coordinates: str = ""
    google_id: str = ""


class CaptchaDetectedException(Exception):
    pass


class DebugLog:
    enabled = False  # Default to disabled
    
    @staticmethod
    def write(message: str):
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        if DebugLog.enabled:
            print(f"[{timestamp}] {message}")
            with open("debug.log", "a", encoding="utf-8") as f:
                f.write(f"[{timestamp}] {message}\n")
    
    @staticmethod
    def clear():
        if DebugLog.enabled:
            with open("debug.log", "w", encoding="utf-8") as f:
                f.write("")


class GoogleMap:
    def __init__(self, debug: bool = False):
        self.debug = debug
        DebugLog.enabled = debug
        self.session = requests.Session()
        self.session.headers.update({
            "Upgrade-Insecure-Requests": "1",
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/143.0.0.0 Safari/537.36",
            "downlink": "10",
            "rtt": "50",
            "sec-ch-prefers-color-scheme": "dark",
            "sec-ch-ua": '"Google Chrome";v="143", "Chromium";v="143", "Not A(Brand";v="24"',
            "sec-ch-ua-arch": '"x86"',
            "sec-ch-ua-bitness": '"64"',
            "sec-ch-ua-form-factors": '"Desktop"',
            "sec-ch-ua-full-version": '"143.0.7499.170"',
            "sec-ch-ua-full-version-list": '"Google Chrome";v="143.0.7499.170", "Chromium";v="143.0.7499.170", "Not A(Brand";v="24.0.0.0"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-model": '""',
            "sec-ch-ua-platform": '"Windows"',
            "sec-ch-ua-platform-version": '"10.0.0"',
            "sec-ch-ua-wow64": "?0"
        })
    
    async def search(self, keyword: str) -> Optional[Place]:
        """Google Maps에서 특정 장소명을 검색합니다. (async version)"""
        return await asyncio.to_thread(self._search_sync, keyword)
    
    def search_sync(self, keyword: str) -> Optional[Place]:
        """Synchronous wrapper for backward compatibility"""
        return self._search_sync(keyword)
    
    def _search_sync(self, keyword: str) -> Optional[Place]:
        """Google Maps에서 특정 장소명을 검색합니다. (internal sync version)"""
        try:
            DebugLog.write(f"검색 시작: {keyword}")
            # 초기 URL로 요청하여 pb 파라미터 추출
            encoded_query = quote(keyword)
            first_url = f"https://www.google.com/maps/search/{encoded_query}/@37.5665,126.9780,13z/data=!3m1!4b1"
            DebugLog.write(f"첫 번째 URL: {first_url}")
            
            first_response = self.session.get(first_url)
            first_response_text = first_response.text
            
            # href 패턴으로 검색 URL 찾기
            href_pattern = r'<link\s+href="(/search\?[^"]*)"'
            href_match = re.search(href_pattern, first_response_text, re.IGNORECASE)
            
            if not href_match:
                # CAPTCHA 체크
                if any(word in first_response_text for word in ["captcha", "recaptcha", "unusual traffic", "sorry/index"]) or \
                   (first_response.url and "sorry" in first_response.url):
                    raise CaptchaDetectedException("Google CAPTCHA 감지됨 - 세션 차단 상태")
                DebugLog.write("href_match를 찾을 수 없음")
                return None
            
            found_url = href_match.group(1)
            pb_match = re.search(r'pb=([^&"]+)', found_url)
            
            if not pb_match:
                DebugLog.write(f"pb_match를 찾을 수 없음. found_url: {found_url}")
                return None
            
            pb_value = pb_match.group(1)
            pb_decoded = unquote(pb_value)
            
            
            # 첫 페이지만 요청 (offset = 0)
            offset = 0
            pb_for_page = self._set_pagination_offset(pb_decoded, offset)
                
            params = {
                "tbm": "map",
                "authuser": "0",
                "hl": "ko",
                "gl": "kr",
                "q": keyword,
                "pb": pb_for_page,
                "start": str(offset)
            }
                
            request_url = f"https://www.google.com/search?{urlencode(params)}"
                
            try:
                response = self.session.get(request_url)
                response_text = response.text
            except Exception:
                return None
                
            # JSON 파싱을 위한 prefix 제거
            cleaned_response = response_text
            if cleaned_response.startswith(")]}'\n"):
                cleaned_response = cleaned_response[5:]
            elif cleaned_response.startswith(")]}'"):
                cleaned_response = cleaned_response[4:]
            else:
                return None
                
            try:
                json_data = json.loads(cleaned_response)
                # 디버그용 JSON 저장 - debug 모드일 때만
                if self.debug:
                    safe_keyword = keyword.replace("/", "_").replace("\\", "_").replace(":", "_")[:30]
                    with open(f"debug_{safe_keyword}.json", "w", encoding="utf-8") as f:
                        json.dump(json_data, f, ensure_ascii=False, indent=2)
                result = self._parse_place_from_json_data(json_data)
                if result:
                    DebugLog.write(f"장소를 찾았습니다: {result.name}")
                else:
                    DebugLog.write("완전한 정보를 찾지 못함")
                return result
            except json.JSONDecodeError as e:
                DebugLog.write(f"JSON 파싱 오류: {e}")
                return None
            
        except CaptchaDetectedException:
            raise
        except Exception:
            return None
    
    def _set_pagination_offset(self, pb: str, offset: int) -> str:
        """pb 파라미터에 페이지네이션 오프셋을 설정합니다."""
        # 기존 !8i 값이 있으면 교체
        if re.search(r'!8i\d+', pb):
            return re.sub(r'!8i\d+', f'!8i{offset}', pb)
        
        # !7i20 뒤에 !8i{offset} 추가
        if re.search(r'(!7i\d+)', pb):
            return re.sub(r'(!7i\d+)', rf'\1!8i{offset}', pb)
        
        # !10b1 앞에 !7i20!8i{offset} 추가
        if re.search(r'(!10b1)', pb):
            return re.sub(r'(!10b1)', rf'!7i20!8i{offset}\1', pb)
        
        # 어디에도 맞지 않으면 끝에 추가
        return pb + f'!7i20!8i{offset}'
    
    def _parse_place_from_json_data(self, json_data) -> Optional[Place]:
        """JSON 데이터에서 장소 정보를 파싱합니다. (특정 장소 검색 전용)"""
        
        # 특정 장소 검색 결과 확인
        if isinstance(json_data, list) and len(json_data) > 0:
            # Case 1: data[0][1][0][14]에 상세 정보가 있는 경우 (카페 르상스, 이도림 등)
            if isinstance(json_data[0], list) and len(json_data[0]) > 1:
                if isinstance(json_data[0][1], list) and len(json_data[0][1]) > 0:
                    first_result = json_data[0][1][0]
                    # Case 1-1: data[0][1][0][14]가 리스트인 경우 (카페 등)
                    if isinstance(first_result, list) and len(first_result) > 14 and isinstance(first_result[14], list):
                        detailed_info = first_result[14]
                        if len(detailed_info) > 11 and isinstance(detailed_info[11], str):
                            # 완전한 정보가 있는 경우만 Place 객체 생성
                            place = Place()
                            place.name = detailed_info[11]
                            place.address = detailed_info[18] if len(detailed_info) > 18 and isinstance(detailed_info[18], str) else None
                            
                            if len(detailed_info) > 9 and isinstance(detailed_info[9], list) and len(detailed_info[9]) >= 4:
                                lat, lng = detailed_info[9][2], detailed_info[9][3]
                                if lat and lng:
                                    place.coordinates = f"{lat},{lng}"
                                else:
                                    place.coordinates = None
                            else:
                                place.coordinates = None
                            
                            place.google_id = detailed_info[10] if len(detailed_info) > 10 and isinstance(detailed_info[10], str) else None
                            place.category = detailed_info[13][0] if len(detailed_info) > 13 and isinstance(detailed_info[13], list) and len(detailed_info[13]) > 0 else None
                            
                            return place
                    
                    # Case 1-2: data[0][1][0][20]에 정보가 있는 경우 (경성대학교 등)
                    elif isinstance(first_result, list) and len(first_result) > 20 and isinstance(first_result[20], list):
                        detailed_info = first_result[20]
                        if len(detailed_info) > 54 and isinstance(detailed_info[54], str):
                            place = Place()
                            place.name = detailed_info[54]
                            place.address = detailed_info[63] if len(detailed_info) > 63 and isinstance(detailed_info[63], str) else None
                            
                            if len(detailed_info) > 47 and isinstance(detailed_info[47], list) and len(detailed_info[47]) >= 4:
                                lat, lng = detailed_info[47][2], detailed_info[47][3]
                                if lat and lng:
                                    place.coordinates = f"{lat},{lng}"
                            
                            place.google_id = detailed_info[53] if len(detailed_info) > 53 and isinstance(detailed_info[53], str) else None
                            
                            if len(detailed_info) > 56 and isinstance(detailed_info[56], list) and len(detailed_info[56]) > 0:
                                place.category = detailed_info[56][0]
                            
                            return place
            
            # Case 2: data[64]에 검색 결과가 있는 경우 (경성대학교 등)
            if len(json_data) > 64 and isinstance(json_data[64], list):
                # 첫 번째 결과는 보통 경성중학교 같은 유사 결과
                # 두 번째 결과(data[64][1])가 정확한 매칭
                if len(json_data[64]) > 1 and isinstance(json_data[64][1], list) and len(json_data[64][1]) > 1:
                    result = json_data[64][1][1]
                    if isinstance(result, list) and len(result) > 18:
                        # 이름 확인 (인덱스 11)
                        if isinstance(result[11], str):
                            place = Place()
                            place.name = result[11]
                            place.address = result[18] if isinstance(result[18], str) else None
                            
                            # 좌표 (인덱스 9)
                            if len(result) > 9 and isinstance(result[9], list) and len(result[9]) >= 4:
                                lat, lng = result[9][2], result[9][3]
                                if lat and lng:
                                    place.coordinates = f"{lat},{lng}"
                            
                            # Google ID (인덱스 10)
                            place.google_id = result[10] if len(result) > 10 and isinstance(result[10], str) else None
                            
                            # 카테고리 (인덱스 13)
                            if len(result) > 13 and isinstance(result[13], list) and len(result[13]) > 0:
                                place.category = result[13][0]
                            
                            return place
        
        # 정상적인 데이터를 찾지 못한 경우 None을 반환
        return None
    
    


# 사용 예시
if __name__ == "__main__":
    async def main():
        google_map = GoogleMap()
        
        try:
            # 검색 예시
            place = await google_map.search("경성대")
            
            if place:
                print(f"\n장소를 찾았습니다.")
                print(f"\n이름: {place.name}")
                print(f"주소: {place.address}")
                print(f"카테고리: {place.category}")
                print(f"좌표: {place.coordinates}")
                print(f"Google ID: {place.google_id}")
            else:
                print("\n완전한 정보를 찾을 수 없습니다.")
            
        except CaptchaDetectedException as e:
            print(f"CAPTCHA 오류: {e}")
        except Exception as e:
            print(f"오류 발생: {e}")
    
    asyncio.run(main())