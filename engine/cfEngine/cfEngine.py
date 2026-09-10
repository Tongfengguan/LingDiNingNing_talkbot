import httpx
import re
from loguru import logger

async def get_cf_info(handle):
    if not re.fullmatch(r"[A-Za-z0-9_.-]{3,24}", handle):
        return "Codeforces 用户名格式无效。"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get("https://codeforces.com/api/user.info", params={"handles": handle})
            if resp.status_code != 200: return f"找不到 {handle}，你是笨蛋吗？"
            
            user = resp.json()["result"][0]
            return (f"【Codeforces 信息】\nHandle: {handle}\n"
                    f"Rating: {user.get('rating', 'Unrated')}\n"
                    f"Rank: {user.get('rank', 'Unrated')}\n"
                    f"Max Rating: {user.get('maxRating', 'Unrated')}")
    except Exception as e:
        logger.error(f"CF Error: {e}")
        return "查询出错了，网络什么的果然很不可靠。"
