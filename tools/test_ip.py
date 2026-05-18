import httpx, asyncio
async def t():
    try:
        r = await httpx.AsyncClient(verify=False, timeout=5).get("https://91.99.169.109/whatsmeow")
        print("STATUS:", r.status_code)
    except Exception as e:
        print("ERROR:", e)
asyncio.run(t())
