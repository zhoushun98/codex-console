"""
IMAP 邮箱服务
支持 Gmail / QQ / 163 / Yahoo / Outlook 等标准 IMAP 协议邮件服务商。
仅用于接收验证码，强制直连（imaplib 不支持代理）。
"""

import email as email_module
import imaplib
import logging
import random
import re
import string
import time
from email.header import decode_header
from email.utils import getaddresses, parsedate_to_datetime
from html import unescape
from typing import Any, Dict, Optional

from .base import BaseEmailService
from ..config.constants import (
    EmailServiceType,
    OPENAI_EMAIL_SENDERS,
    OTP_CODE_PATTERN,
    OTP_CODE_SEMANTIC_PATTERN,
)
from ..config.settings import get_settings

logger = logging.getLogger(__name__)

IMAP_ADDRESS_MODE_SINGLE = "single"
IMAP_ADDRESS_MODE_CATCHALL = "catchall"
IMAP_RECIPIENT_HEADER_KEYS = (
    "To",
    "Delivered-To",
    "X-Original-To",
    "Original-To",
    "Envelope-To",
    "Cc",
)


def get_email_code_settings() -> dict:
    """Return OTP polling settings with safe defaults."""
    try:
        settings = get_settings()
        timeout = max(1, int(getattr(settings, "email_code_timeout", 60) or 60))
        poll_interval = max(1, int(getattr(settings, "email_code_poll_interval", 3) or 3))
    except Exception as e:
        logger.debug("读取 IMAP 验证码设置失败，使用默认值: %s", e)
        timeout = 60
        poll_interval = 3
    return {
        "timeout": timeout,
        "poll_interval": poll_interval,
    }


def _normalize_domain_text(value: Any) -> str:
    return str(value or "").strip().lower().lstrip("@").strip(".")


def normalize_imap_address_mode(config: Optional[Dict[str, Any]]) -> str:
    raw_value = str((config or {}).get("address_mode") or IMAP_ADDRESS_MODE_SINGLE).strip().lower()
    return IMAP_ADDRESS_MODE_CATCHALL if raw_value == IMAP_ADDRESS_MODE_CATCHALL else IMAP_ADDRESS_MODE_SINGLE


def get_imap_generated_domain(config: Optional[Dict[str, Any]]) -> str:
    cfg = config or {}
    domain = _normalize_domain_text(cfg.get("domain"))
    subdomain = _normalize_domain_text(cfg.get("subdomain"))
    if not domain:
        return ""
    return f"{subdomain}.{domain}" if subdomain else domain


def imap_service_matches_account(config: Optional[Dict[str, Any]], account_email: str) -> bool:
    cfg = config or {}
    email_lower = str(account_email or "").strip().lower()
    if not email_lower:
        return False

    if normalize_imap_address_mode(cfg) == IMAP_ADDRESS_MODE_CATCHALL:
        generated_domain = get_imap_generated_domain(cfg)
        return bool(generated_domain and email_lower.endswith(f"@{generated_domain}"))

    cfg_email = str(cfg.get("email") or "").strip().lower()
    return bool(cfg_email and cfg_email == email_lower)


class ImapMailService(BaseEmailService):
    """标准 IMAP 邮箱服务（支持普通模式与 catchall 模式）"""

    def __init__(self, config: Dict[str, Any] = None, name: str = None):
        super().__init__(EmailServiceType.IMAP_MAIL, name)

        cfg = dict(config or {})
        required_keys = ["host", "email", "password"]
        missing_keys = [k for k in required_keys if not cfg.get(k)]
        if missing_keys:
            raise ValueError(f"缺少必需配置: {missing_keys}")

        self.host: str = str(cfg["host"]).strip()
        self.port: int = int(cfg.get("port", 993))
        self.use_ssl: bool = bool(cfg.get("use_ssl", True))
        self.email_addr: str = str(cfg["email"]).strip()
        self.password: str = str(cfg["password"])
        self.timeout: int = int(cfg.get("timeout", 30))
        self.max_retries: int = int(cfg.get("max_retries", 3))
        self.address_mode: str = normalize_imap_address_mode(cfg)
        self.domain: str = _normalize_domain_text(cfg.get("domain"))
        self.subdomain: str = _normalize_domain_text(cfg.get("subdomain"))
        self.generated_domain: str = get_imap_generated_domain(cfg)

        if self.address_mode == IMAP_ADDRESS_MODE_CATCHALL and not self.generated_domain:
            raise ValueError("catchall 模式缺少 domain 配置")

    def _connect(self) -> imaplib.IMAP4:
        """建立 IMAP 连接并登录，返回 mail 对象"""
        if self.use_ssl:
            mail = imaplib.IMAP4_SSL(self.host, self.port)
        else:
            mail = imaplib.IMAP4(self.host, self.port)
            mail.starttls()
        mail.login(self.email_addr, self.password)
        return mail

    def _decode_str(self, value: Any) -> str:
        """解码邮件头部字段"""
        if value is None:
            return ""
        parts = decode_header(value)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(part))
        return " ".join(decoded).strip()

    def _get_text_body(self, msg) -> str:
        """提取邮件正文，兼容 text/plain 与 text/html。"""
        chunks = []
        parts = msg.walk() if msg.is_multipart() else [msg]

        for part in parts:
            if part.get_content_maintype() == "multipart":
                continue
            content_type = (part.get_content_type() or "").lower()
            if content_type not in ("text/plain", "text/html"):
                continue

            payload = part.get_payload(decode=True)
            if not payload:
                continue

            charset = part.get_content_charset() or "utf-8"
            try:
                text = payload.decode(charset, errors="replace")
            except LookupError:
                text = payload.decode("utf-8", errors="replace")

            if content_type == "text/html":
                text = re.sub(r"<[^>]+>", " ", text)
            chunks.append(text)

        body = " ".join(chunks)
        body = unescape(body)
        body = re.sub(r"\s+", " ", body).strip()
        return body

    def _is_openai_sender(self, from_addr: str) -> bool:
        """判断发件人是否为 OpenAI"""
        from_lower = from_addr.lower()
        for sender in OPENAI_EMAIL_SENDERS:
            if sender in from_lower:
                return True
        return False

    def _extract_otp(self, text: str, pattern: Optional[str] = None) -> Optional[str]:
        """从文本中提取 6 位验证码，优先语义匹配，回退简单匹配"""
        semantic_match = re.search(OTP_CODE_SEMANTIC_PATTERN, text, re.IGNORECASE)
        if semantic_match:
            return semantic_match.group(1)
        fallback_pattern = pattern or OTP_CODE_PATTERN
        simple_match = re.search(fallback_pattern, text)
        if simple_match:
            return simple_match.group(1)
        return None

    def _generate_local_part(self, seed: Optional[str] = None) -> str:
        cleaned = re.sub(r"[^a-z0-9]", "", str(seed or "").strip().lower())
        if cleaned and cleaned[0].isalpha() and len(cleaned) >= 1:
            return cleaned[:32]

        first = random.choice(string.ascii_lowercase)
        rest = "".join(random.choices(string.ascii_lowercase + string.digits, k=9))
        return f"{first}{rest}"

    def _extract_fetch_bytes(self, msg_data) -> bytes:
        for item in msg_data or []:
            if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                return bytes(item[1])
        return b""

    def _extract_message_timestamp(self, msg) -> Optional[float]:
        date_str = self._decode_str(msg.get("Date", ""))
        if not date_str:
            return None
        try:
            dt = parsedate_to_datetime(date_str)
            if dt is None:
                return None
            return float(dt.timestamp())
        except Exception:
            return None

    def _clip_text(self, value: Any, limit: int = 160) -> str:
        text = re.sub(r"\s+", " ", str(value or "").strip())
        if len(text) <= limit:
            return text
        return f"{text[: max(0, limit - 3)]}..."

    def _summarize_message(self, msg, msg_id: str, raw_headers_text: str) -> Dict[str, str]:
        recipient_blob = self._extract_recipient_blob(msg)
        recipients = [addr.lower() for _, addr in getaddresses([recipient_blob]) if addr]
        recipients_text = ", ".join(dict.fromkeys(recipients))
        if not recipients_text:
            recipients_text = self._clip_text(recipient_blob or raw_headers_text or "-", limit=160)
        return {
            "msg_id": str(msg_id or "").strip() or "?",
            "subject": self._clip_text(self._decode_str(msg.get("Subject", "")) or "-", limit=120),
            "recipients": self._clip_text(recipients_text or "-", limit=160),
        }

    def _extract_recipient_blob(self, msg) -> str:
        parts = []
        for key in IMAP_RECIPIENT_HEADER_KEYS:
            raw_value = msg.get(key, "")
            decoded = self._decode_str(raw_value)
            if decoded:
                parts.append(decoded)
        return "\n".join(parts).strip()

    def _matches_target_recipient(self, msg, target_email: str, raw_headers_text: str) -> bool:
        target = str(target_email or "").strip().lower()
        if not target:
            return True

        recipient_blob = self._extract_recipient_blob(msg)
        recipient_values = [addr.lower() for _, addr in getaddresses([recipient_blob]) if addr]
        if any(value == target for value in recipient_values):
            return True

        combined = "\n".join(
            part for part in (recipient_blob, raw_headers_text or "") if part
        ).lower()
        if target and target in combined:
            return True

        # 普通单邮箱模式下，少数服务缺少投递头，保持历史兼容。
        return self.address_mode == IMAP_ADDRESS_MODE_SINGLE and not combined

    def create_email(self, config: Dict[str, Any] = None) -> Dict[str, Any]:
        """创建注册邮箱；catchall 模式下本地生成地址，收件仍使用固定 IMAP 收件箱。"""
        request_config = config or {}
        if self.address_mode == IMAP_ADDRESS_MODE_CATCHALL:
            local_part = self._generate_local_part(request_config.get("name"))
            generated_email = f"{local_part}@{self.generated_domain}"
        else:
            generated_email = self.email_addr

        self.update_status(True)
        return {
            "email": generated_email,
            "inbox_email": self.email_addr,
            "service_id": generated_email,
            "id": generated_email,
        }

    def get_verification_code(
        self,
        email: str,
        email_id: str = None,
        timeout: int = 60,
        pattern: str = None,
        otp_sent_at: Optional[float] = None,
    ) -> Optional[str]:
        """轮询 IMAP 收件箱，按目标收件人抓取 OpenAI 验证码。"""
        del email_id

        target_email = str(email or "").strip().lower()
        settings = get_email_code_settings()
        try:
            effective_timeout = max(1, int(timeout)) if timeout is not None else settings["timeout"]
        except (TypeError, ValueError):
            effective_timeout = settings["timeout"]
        poll_interval = max(1, int(settings["poll_interval"]))
        start_time = time.time()
        seen_ids: set[str] = set()
        mail = None
        poll_count = 0

        logger.info(
            "IMAP 开始轮询验证码: target=%s inbox=%s timeout=%ss poll_interval=%ss mode=%s",
            target_email or self.email_addr,
            self.email_addr,
            effective_timeout,
            poll_interval,
            self.address_mode,
        )

        try:
            mail = self._connect()
            mail.select("INBOX")

            while time.time() - start_time < effective_timeout:
                poll_count += 1
                inspected_count = 0
                openai_candidate_count = 0
                try:
                    search_criteria = ("UNSEEN", "ALL")
                    for criteria in search_criteria:
                        status, data = mail.search(None, criteria)
                        if status != "OK" or not data or not data[0]:
                            continue

                        msg_ids = data[0].split()
                        if criteria == "ALL" and len(msg_ids) > 30:
                            msg_ids = msg_ids[-30:]

                        for msg_id in reversed(msg_ids):
                            id_str = msg_id.decode(errors="ignore")
                            if id_str in seen_ids:
                                continue
                            seen_ids.add(id_str)
                            inspected_count += 1

                            status, msg_data = mail.fetch(msg_id, "(RFC822)")
                            if status != "OK" or not msg_data:
                                continue

                            raw_bytes = self._extract_fetch_bytes(msg_data)
                            if not raw_bytes:
                                continue

                            raw_text = raw_bytes.decode("utf-8", errors="replace")
                            raw_headers_text = raw_text.split("\n\n", 1)[0]
                            msg = email_module.message_from_bytes(raw_bytes)
                            summary = self._summarize_message(msg, id_str, raw_headers_text)

                            from_addr = self._decode_str(msg.get("From", ""))
                            if not self._is_openai_sender(from_addr):
                                continue
                            openai_candidate_count += 1

                            message_ts = self._extract_message_timestamp(msg)
                            if otp_sent_at and message_ts is not None and message_ts + 2 < otp_sent_at:
                                logger.info(
                                    "IMAP 跳过邮件: reason=old_mail msg_id=%s target=%s subject=%s recipients=%s criteria=%s",
                                    summary["msg_id"],
                                    target_email or self.email_addr,
                                    summary["subject"],
                                    summary["recipients"],
                                    criteria,
                                )
                                continue
                            if otp_sent_at and message_ts is None and criteria == "ALL":
                                logger.info(
                                    "IMAP 跳过邮件: reason=old_mail msg_id=%s target=%s subject=%s recipients=%s criteria=%s message_ts=unknown",
                                    summary["msg_id"],
                                    target_email or self.email_addr,
                                    summary["subject"],
                                    summary["recipients"],
                                    criteria,
                                )
                                continue

                            if not self._matches_target_recipient(msg, target_email, raw_headers_text):
                                logger.info(
                                    "IMAP 跳过邮件: reason=recipient_mismatch msg_id=%s target=%s subject=%s recipients=%s criteria=%s",
                                    summary["msg_id"],
                                    target_email or self.email_addr,
                                    summary["subject"],
                                    summary["recipients"],
                                    criteria,
                                )
                                continue

                            subject = self._decode_str(msg.get("Subject", ""))
                            body = self._get_text_body(msg)
                            content = "\n".join(part for part in (from_addr, subject, body) if part).strip()
                            code = self._extract_otp(content, pattern=pattern)
                            if not code:
                                logger.info(
                                    "IMAP 跳过邮件: reason=no_otp_found msg_id=%s target=%s subject=%s recipients=%s criteria=%s",
                                    summary["msg_id"],
                                    target_email or self.email_addr,
                                    summary["subject"],
                                    summary["recipients"],
                                    criteria,
                                )
                                continue

                            try:
                                mail.store(msg_id, "+FLAGS", "\\Seen")
                            except Exception:
                                pass
                            self.update_status(True)
                            logger.info(
                                "IMAP 获取验证码成功: target=%s inbox=%s msg_id=%s subject=%s recipients=%s elapsed=%ss round=%s",
                                target_email or self.email_addr,
                                self.email_addr,
                                summary["msg_id"],
                                summary["subject"],
                                summary["recipients"],
                                int(max(0, time.time() - start_time)),
                                poll_count,
                            )
                            return code

                except imaplib.IMAP4.error as e:
                    logger.debug("IMAP 搜索邮件失败: %s", e)
                    try:
                        if mail:
                            mail.logout()
                    except Exception:
                        pass
                    mail = self._connect()
                    mail.select("INBOX")

                if poll_count == 1 or poll_count % 5 == 0:
                    logger.info(
                        "IMAP 轮询中: target=%s inbox=%s round=%s elapsed=%ss inspected=%s openai_candidates=%s",
                        target_email or self.email_addr,
                        self.email_addr,
                        poll_count,
                        int(max(0, time.time() - start_time)),
                        inspected_count,
                        openai_candidate_count,
                    )

                time.sleep(poll_interval)

        except Exception as e:
            logger.warning("IMAP 连接/轮询失败: %s", e)
            self.update_status(False, e)
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

        logger.info(
            "IMAP 等待验证码超时: target=%s inbox=%s timeout=%ss rounds=%s",
            target_email or self.email_addr,
            self.email_addr,
            effective_timeout,
            poll_count,
        )

        return None

    def check_health(self) -> bool:
        """尝试 IMAP 登录并选择收件箱"""
        mail = None
        try:
            mail = self._connect()
            status, _ = mail.select("INBOX")
            return status == "OK"
        except Exception as e:
            logger.warning("IMAP 健康检查失败: %s", e)
            return False
        finally:
            if mail:
                try:
                    mail.logout()
                except Exception:
                    pass

    def list_emails(self, **kwargs) -> list:
        """IMAP 模式返回实际收件箱。"""
        del kwargs
        return [{
            "email": self.email_addr,
            "id": self.email_addr,
            "address_mode": self.address_mode,
            "generated_domain": self.generated_domain or None,
        }]

    def delete_email(self, email_id: str) -> bool:
        """IMAP 模式无需删除逻辑"""
        del email_id
        return True
