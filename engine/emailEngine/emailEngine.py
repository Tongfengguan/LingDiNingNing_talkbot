import asyncio
import hmac
import re
import secrets
import smtplib
import ssl
import time
from dataclasses import dataclass
from email.message import EmailMessage
from email.utils import formataddr

from loguru import logger
from config import config


@dataclass
class Draft:
    token: str
    subject: str
    body: str
    recipient: str
    expires: float


drafts: dict[str, Draft] = {}


def email_requested(message):
    return bool(re.search(r"(?:发|发送|寄|send).{0,20}(?:邮件|邮箱|email)", message, re.I))


def create_draft(user_id, subject, body):
    if user_id != config.ADMIN_QQ or user_id not in config.ALLOWED_QQ:
        return "邮件功能仅管理员可用，未发送邮件。"
    if not all((config.SMTP_USER, config.SMTP_PASSWORD, config.RECEIVER_EMAIL)):
        return "邮件配置不完整，未发送邮件。"
    if not isinstance(subject, str) or not isinstance(body, str):
        raise ValueError("invalid email draft")
    if not subject.strip() or len(subject) > 120 or "\r" in subject or "\n" in subject:
        raise ValueError("invalid subject")
    if not body.strip() or len(body) > 6000:
        raise ValueError("invalid email body")
    token = secrets.token_hex(8)
    drafts[user_id] = Draft(token, subject, body, config.RECEIVER_EMAIL,
                            time.monotonic() + config.EMAIL_DRAFT_TTL)
    return (f"【邮件草稿 · 尚未发送】\n收件人：{config.RECEIVER_EMAIL}\n主题：{subject}\n\n{body}\n\n"
            f"请核对全文，10 分钟内发送 /确认邮件 {token} 才会投递。发送 /取消邮件 可取消。")


def cancel_draft(user_id):
    drafts.pop(user_id, None)


async def confirm_draft(user_id, token):
    if user_id != config.ADMIN_QQ or user_id not in config.ALLOWED_QQ:
        return "没有邮件发送权限。"
    draft = drafts.get(user_id)
    if draft is None or draft.expires < time.monotonic():
        cancel_draft(user_id)
        return "草稿不存在或已过期，请重新生成。"
    if not hmac.compare_digest(token.encode(), draft.token.encode()):
        return "确认码不正确，未发送邮件。"
    # Consume before doing I/O; retries and duplicate events cannot resend the same draft.
    cancel_draft(user_id)
    sent = await asyncio.to_thread(send_email_to_user, draft.subject, draft.body, draft.recipient)
    return "邮件已提交给邮件服务器。" if sent else "邮件投递未确认成功；请先检查收件箱，系统不会自动重发。"


def send_email_to_user(subject, content, to_email):
    if not all((config.SMTP_PASSWORD, config.SMTP_USER, to_email)):
        return False
    try:
        message = EmailMessage()
        message["From"] = formataddr((config.BOT_NAME, config.SMTP_USER))
        message["To"] = to_email
        message["Subject"] = subject
        message.set_content(content)
        context = ssl.create_default_context()
        if config.SMTP_PORT == 465:
            server = smtplib.SMTP_SSL(config.SMTP_HOST, config.SMTP_PORT, timeout=10, context=context)
        else:
            server = smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=10)
        with server:
            if config.SMTP_PORT != 465:
                server.starttls(context=context)
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            refused = server.send_message(message)
            if refused:
                return False
        logger.info("Email accepted by SMTP server")
        return True
    except Exception as exc:
        logger.warning("SMTP delivery failed ({})", type(exc).__name__)
        return False
