import httpx
from loguru import logger

async def get_bili_popular(limit=5):
    """获取B站热门视频，包含封面及完整统计数据（播放、点赞、投币、收藏）"""
    url = "https://api.bilibili.com/x/web-interface/popular"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Referer": "https://www.bilibili.com/"
    }
    try:
        async with httpx.AsyncClient(headers=headers, timeout=15) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            data = resp.json()
            if data.get("code") == 0:
                list_data = data["data"]["list"]
                popular_videos = []
                for item in list_data[:limit]:
                    stat = item.get("stat", {})
                    popular_videos.append({
                        "title": item["title"],
                        "bvid": item["bvid"],
                        "author": item["owner"]["name"],
                        "url": f"https://www.bilibili.com/video/{item['bvid']}",
                        "cover": item["pic"],
                        "view": stat.get("view", 0),
                        "like": stat.get("like", 0),
                        "coin": stat.get("coin", 0),
                        "favorite": stat.get("favorite", 0) # 新增收藏数
                    })
                return popular_videos
            return []
    except Exception as e:
        logger.warning("B站数据获取失败 ({})", type(e).__name__)
        return []
