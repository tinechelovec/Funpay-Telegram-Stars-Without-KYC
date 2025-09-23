
import os
import asyncio
from dotenv import load_dotenv
from pyrogram import Client

load_dotenv()
API_ID = int(os.getenv("API_ID", "0"))
API_HASH = os.getenv("API_HASH", "")

if not API_ID or not API_HASH:
    raise SystemExit("В .env нужны API_ID и API_HASH")

async def main():
    async with Client("telegram", api_id=API_ID, api_hash=API_HASH, workdir="sessions") as app:
        me = await app.get_me()
        print(f"✅ Сессия создана. Вошли как {me.first_name} (id={me.id}).")

if __name__ == "__main__":
    asyncio.run(main())
