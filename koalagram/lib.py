from dataclasses import dataclass, field
from enum import Enum, auto
from curl_cffi import requests
from typing import List, Optional, Tuple
import os
from urllib.parse import urlparse
from groq import Groq
from pydantic import BaseModel, Field, ValidationError
import json
import json_repair
import asyncio
from .map import GoogleMap, Place
from .prompt import LOCATIONS_PROMPT, SUMMARY_PROMPT

GROQ_MODEL = 'llama-3.1-8b-instant'  # GPT OSS 120B model on Groq

class GroqAnalysisResponse(BaseModel):
    """Pydantic model for Groq API response validation"""
    locations: List[str] = Field(default_factory=list, description="List of location names found in the post")
    summary: str = Field(default="", description="Korean language summary of the post")
    
    class Config:
        json_schema_extra = {
            "examples": [{
                "locations": ["이태원", "스타벅스 강남점"],
                "summary": "떠그클럽이 이태원에 새로운 매장을 오픈했습니다. 아디다스와의 협업 제품을 선보이며 많은 관심을 받고 있습니다."
            }]
        }

class FileType(Enum):
    IMAGE = auto()     # jpg, png
    VIDEO = auto()     # mp4, mov

class MediaType(Enum):
    FEED = auto()              # 일반 이미지/영상 게시물
    CAROUSEL = auto()          # 여러 개 (앨범)
    REEL = auto()              # 릴스

@dataclass
class File:
    type: FileType
    index: int
    url: str
    accessibility_caption: str = ""

@dataclass
class Coordinates:
    lat: float
    lng: float

@dataclass
class Location:
    name: str
    address: str = ""
    rating: float = 0.0
    review_count: int = 0  # 리뷰 수
    category: str = ""  # 비류 = category
    coordinates: Coordinates = None

@dataclass
class AnalysisResult:
    summary: Optional[str] = None
    locations: List[Location] = field(default_factory=list)

@dataclass
class User:
    pk: str
    name: str  # username
    nickname: str  # full_name
    profile_url: str

@dataclass
class Media:
    user: User
    pk: str
    files: List[File]
    type: MediaType
    caption: str = ""
    accessibility_caption: str = ""
    _koalagram: Optional['Koalagram'] = field(default=None, repr=False)
    
    
    async def analyze(self) -> AnalysisResult:
        """Get summary and locations in a single Gemini API request"""
        summary = None
        locations = []
        
        # Check if we need to process anything
        if not self.caption:
            return AnalysisResult(summary=summary, locations=locations)
            
        # Check if Groq API key is available
        if not self._koalagram or not self._koalagram.groq_api_key:
            return AnalysisResult(summary=summary, locations=locations)
        
        # Format additional information for summary
        additional_info = ""
        
        # Main accessibility caption
        if self.accessibility_caption:
            additional_info = f"\nMain accessibility caption: {self.accessibility_caption}"
            if self._koalagram.debug:
                print(f"📸 메인 접근성 캡션: {self.accessibility_caption}")
        
        # Individual file accessibility captions (for carousel)
        file_captions = []
        for file in self.files:
            if file.accessibility_caption:
                file_captions.append(f"Image {file.index + 1}: {file.accessibility_caption}")
                if self._koalagram.debug:
                    print(f"  📷 이미지 {file.index + 1} 접근성 캡션: {file.accessibility_caption}")
        
        if file_captions:
            additional_info += "\n\nIndividual image accessibility captions:\n" + "\n".join(file_captions)
        
        # Create combined prompt
        locations_prompt = LOCATIONS_PROMPT.format(caption=self.caption, additional=additional_info)
        summary_prompt = SUMMARY_PROMPT.format(caption=self.caption, additional=additional_info)
        
        # Get schema and example
        schema = GroqAnalysisResponse.model_json_schema()
        example = GroqAnalysisResponse.Config.json_schema_extra["examples"][0]
        
        combined_prompt = f"""You are analyzing an Instagram post. Please perform TWO tasks:

1. Extract location names from the caption and image descriptions
2. Create a summary of the post

=== TASK 1: EXTRACT LOCATIONS ===
{locations_prompt}

=== TASK 2: CREATE SUMMARY ===
{summary_prompt}

You MUST return a valid JSON object with this EXACT schema:
{json.dumps(schema, ensure_ascii=False, indent=2)}

Example response:
{json.dumps(example, ensure_ascii=False, indent=2)}

RULES:
1. Return ONLY the JSON object, no additional text
2. Ensure all strings are properly terminated with quotes
3. Use double quotes for JSON strings
4. Keep summary under 500 characters
"""
        
        try:
            client = Groq(api_key=self._koalagram.groq_api_key)
            
            if self._koalagram.debug:
                print(f"Using Groq model: {GROQ_MODEL}")
                print(f"Prompt length: {len(combined_prompt)} chars")
            
            response = await asyncio.to_thread(
                client.chat.completions.create,
                model=GROQ_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": "You are a JSON-only response bot. You MUST:\n1. Return ONLY valid JSON\n2. Start with { and end with }\n3. Use double quotes for all strings\n4. Ensure all strings are properly terminated\n5. Follow the exact schema provided\n6. Never include explanations or additional text"
                    },
                    {
                        "role": "user",
                        "content": combined_prompt
                    }
                ],
                temperature=0.1,
                max_tokens=1024
            )
            
            if not response.choices or not response.choices[0].message:
                if self._koalagram.debug:
                    print("No response from Groq API")
                return AnalysisResult(summary=summary, locations=locations)
            
            json_text = response.choices[0].message.content.strip()
            if self._koalagram.debug:
                print(f"Raw Groq response: {json_text}")
            
            # Try to parse with Pydantic first
            try:
                # Try direct parsing
                result = GroqAnalysisResponse.model_validate_json(json_text)
            except ValidationError:
                # Clean up JSON if direct parsing fails
                if '```json' in json_text:
                    start = json_text.find('```json') + 7
                    end = json_text.find('```', start)
                    if end != -1:
                        json_text = json_text[start:end].strip()
                elif '{' in json_text:
                    start = json_text.find('{')
                    end = json_text.rfind('}') + 1
                    if end > start:
                        json_text = json_text[start:end]
                
                if self._koalagram.debug:
                    print(f"Cleaned JSON: {json_text}")
                
                try:
                    # Try parsing cleaned JSON
                    result = GroqAnalysisResponse.model_validate_json(json_text)
                except ValidationError:
                    # Fallback: try to repair and parse JSON
                    try:
                        if self._koalagram.debug:
                            print("Attempting to repair JSON...")
                        repaired_json = json_repair.repair_json(json_text)
                        if self._koalagram.debug:
                            print(f"Repaired JSON: {repaired_json}")
                        data = json.loads(repaired_json)
                        result = GroqAnalysisResponse.model_validate(data)
                    except (json.JSONDecodeError, ValidationError) as e:
                        if self._koalagram.debug:
                            print(f"Failed to parse response: {e}")
                        # Return empty result on failure
                        result = GroqAnalysisResponse()
            
            location_names = result.locations
            summary = result.summary
            
            # Search locations using Google Maps if we found any
            if location_names:
                # Initialize Google Maps searcher with debug setting
                google_map = GoogleMap(debug=self._koalagram.debug)
                
                # Search each location name using Google Maps
                seen_addresses = set()  # To avoid duplicates
                
                # Create tasks for parallel location searches
                search_tasks = []
                for name in location_names:
                    search_tasks.append(google_map.search(name))
                
                # Run all searches in parallel
                search_results = await asyncio.gather(*search_tasks, return_exceptions=True)
                
                # Process results
                for name, result in zip(location_names, search_results):
                    if isinstance(result, Exception):
                        if self._koalagram.debug:
                            print(f"Error searching for '{name}': {result}")
                        continue
                    
                    place = result
                    if place and place.address and place.address not in seen_addresses:
                        seen_addresses.add(place.address)
                        
                        # Convert Place to Location
                        coords = None
                        if place.coordinates:
                            lat, lng = place.coordinates.split(',')
                            coords = Coordinates(lat=float(lat), lng=float(lng))
                        
                        locations.append(Location(
                            name=place.name,
                            address=place.address,
                            rating=0.0,  # Google Maps API doesn't return rating
                            review_count=0,
                            category=place.category or "",
                            coordinates=coords
                        ))
            
        except Exception as e:
            if self._koalagram.debug:
                print(f"Error in combined analysis: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
        
        return AnalysisResult(summary=summary, locations=locations)
    
    # Keep synchronous versions for backward compatibility
    def summary(self) -> Optional[str]:
        """Get a summary of the media post (synchronous wrapper)"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self.analyze())
            return result.summary
        finally:
            loop.close()
    
    def locations(self) -> List[Location]:
        """Extract location names from caption and search them using Google Maps (synchronous wrapper)"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            result = loop.run_until_complete(self.analyze())
            return result.locations
        finally:
            loop.close()

class Koalagram:
    def __init__(self, groq_api_key: str = None, debug: bool = False):
        self.session = requests.Session(impersonate="safari")
        self.groq_api_key = groq_api_key
        self.debug = debug

    async def fetch(self, url: str) -> Media:
        """Fetch media asynchronously"""
        return await asyncio.to_thread(self._fetch_sync, url)
    
    def fetch_sync(self, url: str) -> Media:
        """Synchronous wrapper for backward compatibility"""
        return self._fetch_sync(url)
    
    def _fetch_sync(self, url: str) -> Media:
        # Clean URL - remove query params and trailing slash
        url = url.replace('/reels/', '/reel/')
        clean_url = url.split('?')[0].rstrip('/')
        
        response = self.session.get(clean_url)
        if response.status_code != 200:
            raise Exception(f"Failed to fetch: {response.status_code}")
        
        html = response.text
        
        # Extract shortcode from URL
        if '/p/' in url:
            shortcode = url.split('/p/')[1].split('?')[0].strip('/')
        elif '/reel/' in url:
            shortcode = url.split('/reel/')[1].split('?')[0].strip('/')
        elif '/reels/' in url:
            shortcode = url.split('/reels/')[1].split('?')[0].strip('/')
        else:
            raise ValueError("Invalid Instagram URL")
        
        # Find all JSON scripts
        all_jsons = []
        pos = 0
        
        while True:
            start = html.find('data-content-len="', pos)
            if start == -1:
                break
            
            len_start = start + 18
            len_end = html.find('"', len_start)
            content_len = int(html[len_start:len_end])
            
            json_start = html.find('>', len_end) + 1
            json_end = html.find('</script>', json_start)
            
            if json_end != -1:
                json_content = html[json_start:json_end]
                all_jsons.append((content_len, json_content))
            
            pos = json_end if json_end != -1 else len_end
        
        # Find matching JSON
        for size, json_content in all_jsons:
            try:
                data = json.loads(json_content)
                json_str = json.dumps(data)

                if f'"code":"{shortcode}"' in json_str or f'"code": "{shortcode}"' in json_str:
                    # Look for xdt_api__v1__media__shortcode__web_info structure
                    if 'xdt_api__v1__media__shortcode__web_info' in json_str:
                        # Find the API response data
                        api_data = self._find_api_response(data)
                        if api_data:
                            return self._parse_media_from_api(api_data)

                    # Fallback to old method
                    matching_item = self._find_matching_item(data, shortcode)
                    if matching_item:
                        return self._parse_media(matching_item)

            except json.JSONDecodeError:
                continue
                
        raise Exception("Could not find media data")
    
    def _find_matching_item(self, obj, target_code):
        """code == target_code 인 item을 찾는다.

        인스타그램 응답에는 같은 code를 가진 item이 여럿 있을 수 있다.
        예: 릴스 페이지의 XIGPolarisVideoMedia(게이팅/댓글 메타데이터) item은
        code는 일치하지만 video_versions/carousel_media 같은 미디어 필드가 없다.
        단순히 "첫 번째 매칭"을 반환하면 이 메타데이터 item이 잡혀서 files=[] 가 된다.
        따라서 미디어 필드(video_versions / carousel_media / image_versions2)를
        가진 item을 우선적으로 반환하고, 없을 때만 일반 매칭을 반환한다.
        """
        # 1순위: 미디어 필드를 가진 매칭 item
        result = self._find_item_with_media(obj, target_code)
        if result is not None:
            return result
        # 2순위: 기존 동작 (code만 일치하는 첫 item)
        return self._find_item_by_code(obj, target_code)

    def _find_item_by_code(self, obj, target_code):
        if isinstance(obj, dict):
            if obj.get('code') == target_code:
                return obj
            for value in obj.values():
                result = self._find_item_by_code(value, target_code)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._find_item_by_code(item, target_code)
                if result:
                    return result
        return None

    def _find_item_with_media(self, obj, target_code):
        """code가 일치하면서 실제 미디어 필드를 가진 item을 우선 반환."""
        if isinstance(obj, dict):
            if obj.get('code') == target_code and self._has_media_fields(obj):
                return obj
            for value in obj.values():
                result = self._find_item_with_media(value, target_code)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._find_item_with_media(item, target_code)
                if result:
                    return result
        return None

    @staticmethod
    def _has_media_fields(obj) -> bool:
        """실제 미디어 데이터(파일 URL을 뽑아낼 수 있는 필드)가 있는지."""
        if not isinstance(obj, dict):
            return False
        # 1) carousel (여러 장)
        if obj.get('carousel_media'):
            return True
        # 2) 단일 비디오
        vv = obj.get('video_versions')
        if isinstance(vv, list) and vv:
            return True
        # 3) 단일 이미지
        iv2 = obj.get('image_versions2')
        if isinstance(iv2, dict) and isinstance(iv2.get('candidates'), list) and iv2['candidates']:
            return True
        return False
    
    def _find_api_response(self, obj):
        """Find the data object containing xdt_api__v1__media__shortcode__web_info"""
        if isinstance(obj, dict):
            # Look for the parent 'data' object that contains both the API and user
            if 'xdt_api__v1__media__shortcode__web_info' in obj and isinstance(obj.get('xdt_api__v1__media__shortcode__web_info'), dict):
                # Return the parent 'data' object, not just the API part
                return obj
            for value in obj.values():
                result = self._find_api_response(value)
                if result:
                    return result
        elif isinstance(obj, list):
            for item in obj:
                result = self._find_api_response(item)
                if result:
                    return result
        return None
    
    def _parse_media_from_api(self, data_obj) -> Media:
        """Parse media from API response structure"""
        # Get the API data
        api_data = data_obj.get('xdt_api__v1__media__shortcode__web_info', {})
        
        # Try to get user from data level first (some responses have it here)
        user_info = data_obj.get('user', {})
        
        # If not found at data level, check inside items[0]
        if not user_info and 'items' in api_data and api_data['items']:
            item = api_data['items'][0]
            if 'user' in item:
                user_info = item['user']
        
        user = User(
            pk=str(user_info.get('pk', user_info.get('id', ''))),
            name=user_info.get('username', ''),
            nickname=user_info.get('full_name', ''),
            profile_url=user_info.get('profile_pic_url', '')
        )
        
        # Get the first item (should be our media)
        if 'items' in api_data and api_data['items']:
            item = api_data['items'][0]
            
            # Parse the media item
            media_type = MediaType.FEED
            files = []
            
            # Check if it's a carousel
            if 'carousel_media' in item and item['carousel_media']:
                media_type = MediaType.CAROUSEL
                for idx, media in enumerate(item['carousel_media']):
                    file_type = FileType.VIDEO if media.get('media_type') == 2 else FileType.IMAGE
                    
                    if file_type == FileType.VIDEO and 'video_versions' in media:
                        url = media['video_versions'][0]['url']
                    elif 'image_versions2' in media and 'candidates' in media['image_versions2']:
                        url = media['image_versions2']['candidates'][0]['url']
                    else:
                        continue
                    
                    # Get accessibility caption for each carousel item
                    carousel_accessibility_caption = media.get('accessibility_caption', '')
                    
                    files.append(File(
                        type=file_type,
                        index=idx,
                        url=url,
                        accessibility_caption=carousel_accessibility_caption
                    ))
            else:
                # Single media
                if item.get('product_type') == 'clips':
                    media_type = MediaType.REEL
                    file_type = FileType.VIDEO
                elif item.get('media_type') == 2:
                    media_type = MediaType.FEED
                    file_type = FileType.VIDEO
                else:
                    media_type = MediaType.FEED
                    file_type = FileType.IMAGE
                
                url = ''
                if file_type == FileType.VIDEO:
                    if 'video_versions' in item and item['video_versions']:
                        url = item['video_versions'][0]['url']
                else:
                    if 'image_versions2' in item and 'candidates' in item['image_versions2']:
                        url = item['image_versions2']['candidates'][0]['url']
                        
                if url:
                    files.append(File(
                        type=file_type,
                        index=0,
                        url=url,
                        accessibility_caption=item.get('accessibility_caption', '')  # Use the main accessibility caption for single media
                    ))
            
            # Extract caption
            caption = ""
            if 'caption' in item:
                if isinstance(item['caption'], dict):
                    caption = item['caption'].get('text', '')
                elif isinstance(item['caption'], str):
                    caption = item['caption']
            
            # Extract accessibility caption
            accessibility_caption = item.get('accessibility_caption', '')
            
            return Media(
                user=user,
                pk=str(item.get('pk', item.get('id', ''))),
                files=files,
                type=media_type,
                caption=caption,
                accessibility_caption=accessibility_caption,
                _koalagram=self
            )
        
        # Fallback if no items found
        return Media(user=user, pk='', files=[], type=MediaType.FEED, caption="", accessibility_caption="", _koalagram=self)
    
    def _parse_media(self, item) -> Media:
        # User info - try different possible fields
        user_info = item.get('owner', item.get('user', {}))
        user = User(
            pk=str(user_info.get('id', user_info.get('pk', ''))),
            name=user_info.get('username', ''),
            nickname=user_info.get('full_name', ''),
            profile_url=user_info.get('profile_pic_url', '')
        )
        
        # Media type
        media_type = MediaType.FEED
        files = []
        
        # Check if it's a carousel first by looking for carousel_media
        if 'carousel_media' in item and item['carousel_media']:
            media_type = MediaType.CAROUSEL
            for idx, media in enumerate(item['carousel_media']):
                file_type = FileType.VIDEO if media.get('media_type') == 2 else FileType.IMAGE
                
                if file_type == FileType.VIDEO and 'video_versions' in media:
                    url = media['video_versions'][0]['url']
                elif 'image_versions2' in media and 'candidates' in media['image_versions2']:
                    url = media['image_versions2']['candidates'][0]['url']
                else:
                    continue
                
                # Get accessibility caption for each carousel item
                carousel_accessibility_caption = media.get('accessibility_caption', '')
                
                files.append(File(
                    type=file_type,
                    index=idx,
                    url=url,
                    accessibility_caption=carousel_accessibility_caption
                ))
                
        elif 'edge_sidecar_to_children' in item:
            media_type = MediaType.CAROUSEL
            edges = item['edge_sidecar_to_children']['edges']
            for idx, edge in enumerate(edges):
                node = edge['node']
                file_type = FileType.VIDEO if 'Video' in node.get('__typename', '') else FileType.IMAGE
                
                if 'display_url' in node:
                    url = node['display_url']
                elif 'video_url' in node:
                    url = node['video_url']
                else:
                    continue
                
                # Get accessibility caption for each edge node
                edge_accessibility_caption = node.get('accessibility_caption', '')
                
                files.append(File(
                    type=file_type,
                    index=idx,
                    url=url,
                    accessibility_caption=edge_accessibility_caption
                ))
                
        else:
            # Single media (feed or reel)
            # Determine type based on product_type and media_type
            if item.get('product_type') == 'clips':
                media_type = MediaType.REEL
                file_type = FileType.VIDEO
            elif item.get('product_type') == 'feed' and item.get('media_type') == 2:
                # Feed video
                media_type = MediaType.FEED
                file_type = FileType.VIDEO
            elif item.get('product_type') == 'feed' and item.get('media_type') == 1:
                # Feed image
                media_type = MediaType.FEED
                file_type = FileType.IMAGE
            else:
                # Default based on media_type
                media_type = MediaType.FEED
                file_type = FileType.VIDEO if item.get('media_type') == 2 else FileType.IMAGE
            
            # Get URL based on file type
            url = ''
            if file_type == FileType.VIDEO:
                if 'video_versions' in item and item['video_versions']:
                    url = item['video_versions'][0]['url']
                elif 'video_url' in item:
                    url = item['video_url']
            else:
                if 'image_versions2' in item and 'candidates' in item['image_versions2']:
                    url = item['image_versions2']['candidates'][0]['url']
                elif 'display_url' in item:
                    url = item['display_url']
                    
            if url:
                files.append(File(
                    type=file_type,
                    index=0,
                    url=url,
                    accessibility_caption=item.get('accessibility_caption', '')  # Use the main accessibility caption for single media
                ))
        
        # Extract caption
        caption = ""
        if 'caption' in item:
            if isinstance(item['caption'], dict):
                caption = item['caption'].get('text', '')
            elif isinstance(item['caption'], str):
                caption = item['caption']
        
        # Extract accessibility caption
        accessibility_caption = item.get('accessibility_caption', '')
        
        return Media(
            user=user,
            pk=item.get('id', item.get('pk', '')),
            files=files,
            type=media_type,
            caption=caption,
            accessibility_caption=accessibility_caption,
            _koalagram=self
        )
    
    async def download(self, media: Media, folder: str = "downloads"):
        """Download all files from a Media object asynchronously"""
        # Create folder if it doesn't exist
        os.makedirs(folder, exist_ok=True)
        
        # Create subfolder for this media
        media_folder = os.path.join(folder, f"{media.user.name}_{media.pk}")
        os.makedirs(media_folder, exist_ok=True)
        
        # Create download tasks
        download_tasks = []
        for file in media.files:
            # Determine extension based on file type
            ext = '.mp4' if file.type == FileType.VIDEO else '.jpg'
            filename = f"{file.index + 1:02d}{ext}"
            filepath = os.path.join(media_folder, filename)
            
            # Add download task
            download_tasks.append(self._download_file(file.url, filepath, filename))
        
        # Execute all downloads in parallel
        results = await asyncio.gather(*download_tasks, return_exceptions=True)
        
        downloaded = []
        for result in results:
            if isinstance(result, str):
                downloaded.append(result)
            elif isinstance(result, Exception) and self.debug:
                print(f"Download error: {result}")
        
        return downloaded
    
    async def _download_file(self, url: str, filepath: str, filename: str) -> str:
        """Download a single file asynchronously"""
        try:
            response = await asyncio.to_thread(self.session.get, url)
            if response.status_code == 200:
                with open(filepath, 'wb') as f:
                    f.write(response.content)
                if self.debug:
                    print(f"Downloaded: {filename}")
                return filepath
            else:
                raise Exception(f"Failed to download {filename}: {response.status_code}")
        except Exception as e:
            raise Exception(f"Error downloading {filename}: {e}")
    
    def download_sync(self, media: Media, folder: str = "downloads"):
        """Synchronous wrapper for backward compatibility"""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(self.download(media, folder))
        finally:
            loop.close()