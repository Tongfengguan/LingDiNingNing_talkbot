import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    DEEPSEEK_API_KEY = os.getenv("DEEPSEEK_API_KEY")
    DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com/v1")
    DEEPSEEK_MODEL = "deepseek-chat"

    BOT_NAME = os.getenv("BOT_NAME", "绫地宁宁")
    BOT_PORT = int(os.getenv("BOT_PORT", 8080))
    BOT_HOST = os.getenv("BOT_HOST", "127.0.0.1")
    ADMIN_QQ = os.getenv("ADMIN_QQ", "").strip()
    ALLOWED_QQ = frozenset(filter(None, (q.strip() for q in os.getenv("ALLOWED_QQ", ADMIN_QQ).split(","))))
    WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")
    DASHBOARD_TOKEN = os.getenv("DASHBOARD_TOKEN", "")
    MAX_WEBHOOK_BYTES = 65536
    MAX_MESSAGE_CHARS = 8000
    MAX_HISTORY_CHARS = 16000
    EVENT_MAX_AGE = 300
    QUEUE_SIZE = 64
    WORKERS = 4
    RATE_LIMIT = 12
    RATE_WINDOW = 60
    MAX_IMAGE_BYTES = 5 * 1024 * 1024
    MAX_IMAGE_PIXELS = 16_000_000
    MAX_IMAGE_STORE_BYTES = 256 * 1024 * 1024
    MAX_DOC_BYTES = 20 * 1024 * 1024
    MAX_DOC_CHARS = 1_000_000
    MAX_DOC_FILES = 200
    RAG_CHUNK_SIZE = 650
    RAG_CHUNK_OVERLAP = 100
    RAG_VECTOR_WEIGHT = 0.65
    RAG_KEYWORD_WEIGHT = 0.25
    RAG_FRESHNESS_WEIGHT = 0.10
    RAG_CANDIDATES = 12
    MAX_CHAT_CHUNKS_PER_USER = 2000
    EMAIL_DRAFT_TTL = 600
    AUTOMATION_PATH = os.getenv("AUTOMATION_PATH", "data/automations.json")

    NAPCAT_URL = os.getenv("NAPCAT_URL", "http://localhost:3000")
    NAPCAT_TOKEN = os.getenv("NAPCAT_TOKEN", "")

    MAX_HISTORY = 20
    TEMPERATURE = 0.9
    MAX_TOKENS = 1500

    MIN_REPLY_DELAY = 0.5
    MAX_REPLY_DELAY = 2.0

    # 邮件配置
    SMTP_HOST = os.getenv("SMTP_HOST", "smtp.qq.com")
    SMTP_PORT = int(os.getenv("SMTP_PORT", 465))
    SMTP_USER = os.getenv("SMTP_USER") 
    SMTP_PASSWORD = os.getenv("SMTP_PASSWORD") 
    RECEIVER_EMAIL = os.getenv("RECEIVER_EMAIL") 

    def validate(self):
        if len(self.WEBHOOK_SECRET) < 32:
            raise ValueError("WEBHOOK_SECRET 必须配置为至少 32 字符，并与 NapCat HTTP 客户端 token 一致")
        if not self.ALLOWED_QQ or any(not q.isascii() or not q.isdigit() or int(q) <= 0 for q in self.ALLOWED_QQ):
            raise ValueError("请设置 ADMIN_QQ 或 ALLOWED_QQ（逗号分隔的 QQ 号）")
        if self.ADMIN_QQ and self.ADMIN_QQ not in self.ALLOWED_QQ:
            raise ValueError("ADMIN_QQ 必须包含在 ALLOWED_QQ 中")
        if not self.DEEPSEEK_API_KEY:
            raise ValueError("请配置 DEEPSEEK_API_KEY")
        if self.DASHBOARD_TOKEN and len(self.DASHBOARD_TOKEN) < 32:
            raise ValueError("DASHBOARD_TOKEN 至少需要 32 字符；留空则关闭控制台")

config = Config()
