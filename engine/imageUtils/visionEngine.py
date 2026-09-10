import httpx
import os
import base64
from loguru import logger
from engine.imageUtils.imageUtils import fetch_image_bytes

DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY")

async def analyze_image(image_url):
    """调用 Qwen-VL 分析图片，重点识别文字和情绪梗"""
    if not DASHSCOPE_API_KEY:
        return "（宁宁没带眼镜，看不清细节）"

    try:
        raw = await fetch_image_bytes(image_url)
        image_url = "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
    except Exception as exc:
        logger.warning("图片读取被拒绝或失败 ({})", type(exc).__name__)
        return "图片无法安全读取，请重新发送。"

    url = "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation"
    headers = {
        "Authorization": f"Bearer {DASHSCOPE_API_KEY}",
        "Content-Type": "application/json"
    }
    
    payload = {
        "model": "qwen-vl-plus",
        "input": {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"image": image_url},
                        {"text": "这是一张QQ聊天中的图片或表情包。请识别并描述图中的所有文字、主体动作及神态，并概括这张图想表达的'梗'或情绪。回复需简练。"}
                    ]
                }
            ]
        }
    }

    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(url, headers=headers, json=payload)
            resp.raise_for_status()
            result = resp.json()
            description = result['output']['choices'][0]['message']['content'][0]['text']
            return description
    except Exception as e:
        logger.warning("视觉分析失败 ({})", type(e).__name__)
        return "（刚才眼睛花了一下，没看清那张图的内容呢）"
