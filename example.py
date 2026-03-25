import asyncio
import os
from dotenv import load_dotenv
from koalagram import Koalagram

# Load environment variables
load_dotenv()

# Get API key from environment variable
GROQ_API_KEY = os.getenv('GROQ_API_KEY')
DEBUG = os.getenv('DEBUG', 'False').lower() == 'true'

async def main():
    # Check if API key is set
    if not GROQ_API_KEY:
        print("Error: GROQ_API_KEY not found in .env file")
        print("Please set your Groq API key in the .env file")
        return
    
    # Create Koalagram instance with Groq API
    client = Koalagram(groq_api_key=GROQ_API_KEY, debug=DEBUG)
    
    # Example Instagram URL
    url = input("Enter Instagram URL: ").strip()
    
    if not url:
        print("No URL provided, using example URL")
        url = "https://www.instagram.com/p/EXAMPLE_CODE/"
    
    try:
        # Fetch media info
        print(f"\nFetching: {url}")
        media = await client.fetch(url)
        
        print(f"\n📊 Media Info:")
        print(f"  Type: {media.type.name}")
        print(f"  User: @{media.user.name} ({media.user.nickname})")
        print(f"  Files: {len(media.files)} items")
        
        # Analyze the post (uses Groq for AI analysis)
        print("\n🔄 Analyzing...")
        result = await media.analyze()
        
        if result.summary:
            print(f"\n📋 Summary:\n{result.summary}")
        
        if result.locations:
            print(f"\n📍 Locations found:")
            for loc in result.locations:
                print(f"  - {loc.name}")
                if loc.address:
                    print(f"    Address: {loc.address}")
        else:
            print("\n📍 No locations found")
            
        # Ask if user wants to download
        download = input("\n💾 Download media files? (y/n): ").strip().lower()
        if download == 'y':
            downloaded_files = await client.download(media)
            print(f"\n✅ Downloaded {len(downloaded_files)} files")
        
    except Exception as e:
        print(f"\n❌ Error: {e}")
        if DEBUG:
            import traceback
            traceback.print_exc()

# Run the async main function
if __name__ == "__main__":
    print("🐨 Koalagram - Instagram Media Analyzer")
    print("=" * 40)
    asyncio.run(main())