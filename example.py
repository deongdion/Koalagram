import asyncio
import os

from dotenv import load_dotenv

from koalagram import (
    FileType,
    Koalagram,
    KoalagramError,
    MediaGatedError,
    MediaNotFoundError,
    RateLimitError,
)

load_dotenv()

DEBUG = os.getenv("DEBUG", "False").lower() == "true"


async def main():
    client = Koalagram(debug=DEBUG)

    url = input("Enter Instagram URL: ").strip()
    if not url:
        print("No URL provided, using example URL")
        url = "https://www.instagram.com/p/EXAMPLE_CODE/"

    try:
        print(f"\nFetching: {url}")
        media = await client.fetch(url)
    except MediaNotFoundError:
        print("\n❌ 게시물을 찾을 수 없습니다 (삭제됐거나 비공개 계정)")
        return
    except MediaGatedError as e:
        print(f"\n🔒 비로그인으로는 볼 수 없는 게시물입니다: {e.ruling.title}")
        if e.ruling.description:
            print(f"   {e.ruling.description}")
        return
    except RateLimitError as e:
        print(f"\n⏳ 조회 한도를 넘겼습니다: {e}")
        return
    except KoalagramError as e:
        print(f"\n❌ Error: {type(e).__name__}: {e}")
        if DEBUG:
            import traceback
            traceback.print_exc()
        return

    print("\n📊 Media Info:")
    print(f"  Type: {media.type.value}")
    nickname = f" ({media.user.nickname})" if media.user.nickname else ""
    print(f"  User: @{media.user.name}{nickname}")
    print(f"  Files: {len(media.files)} items")
    print(f"  ❤️ Likes: {media.like_count:,}")
    print(f"  💬 Comments: {media.comment_count:,}")
    if media.taken_at_dt:
        print(f"  📅 Posted: {media.taken_at_dt:%Y-%m-%d %H:%M UTC}")
    if media.location:
        print(f"  📍 Location: {media.location.name}")

    print("\n🔗 Media links:")
    for file in media.files:
        icon = "🎬" if file.type is FileType.VIDEO else "🖼️"
        size = f" {file.width}x{file.height}" if file.width else ""
        print(f"  {icon} [{file.index + 1}] {file.type.value}{size}: {file.url}")

    if media.video_files:
        print(f"\n🎬 Video links ({len(media.video_files)}):")
        for i, file in enumerate(media.video_files, 1):
            print(f"  {i}. {file.best_video_url()}")
            for version in file.video_versions:
                dims = f"{version.width}x{version.height}" if version.width else "크기미상"
                print(f"       type={version.type_id} {dims}")
    else:
        print("\n🎬 No video files in this post")

    if media.caption:
        print(f"\n📋 Caption:\n{media.caption}")


if __name__ == "__main__":
    print("🐨 Koalagram - Instagram Media Analyzer")
    print("=" * 40)
    asyncio.run(main())
