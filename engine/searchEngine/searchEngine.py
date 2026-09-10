from duckduckgo_search import DDGS
from loguru import logger

def search_web(query, max_results=5):
    try:
        with DDGS(timeout=10) as ddgs:
            results = [f"Title: {r['title']}\nURL: {r.get('href', '')}\nSnippet: {r['body']}" for r in ddgs.text(query, region='wt-wt', max_results=max_results)]
            return "\n\n".join(results[:max_results]) if results else "No results found."
    except Exception as e:
        logger.warning("Search failed ({})", type(e).__name__)
        return "搜索暂时不可用。"

def search_image_url(query):
    """搜索一张图片的 URL"""
    try:
        with DDGS(timeout=10) as ddgs:
            results = list(ddgs.images(f"{query} 表情包", region='wt-wt', max_results=1))
            return results[0]['image'] if results else None
    except Exception as e:
        logger.warning("Image search failed ({})", type(e).__name__)
        return None
