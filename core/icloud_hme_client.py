# -*- coding: utf-8 -*-
"""
iCloud Hide My Email (HME) 别名邮箱源。

对接本地 icloud-hme 服务（https://github.com/xiaozhou26/icloud-hme）：
  - pick_account()       调 POST /api/create 生成一个新别名 @icloud.com
  - fetch_latest_otp()   调 GET /api/inbox 读取转发到该别名的 ChatGPT 验证码
  - get_account_context() 判断邮箱是否属于本池
  - release_account()     可选：HME 别名可复用，一般无需回收

icloud-hme 服务默认监听 :8081，账号通过其 API 预先添加（含 Cookie 或 SRP 登录）。
"""
import logging
import re
import time

import requests

from config import email as _email_cfg

logger = logging.getLogger(__name__)


class ICloudHmeError(RuntimeError):
    pass


class ICloudHmeEmailAccount:
    """HME 别名邮箱（简单值对象，字段名对齐 email_provider 的通用用法）。"""

    __slots__ = ("email", "account_id", "label")

    def __init__(self, email: str, account_id: str = "", label: str = ""):
        self.email = email
        self.account_id = account_id
        self.label = label


# 进程内缓存：邮箱 -> 账号ID（避免每次重复查询）
_CONTEXT_CACHE: dict[str, ICloudHmeEmailAccount] = {}


def _api_base() -> str:
    return str(getattr(_email_cfg, "ICLOUD_HME_API_BASE", "http://127.0.0.1:8081") or "").strip() or "http://127.0.0.1:8081"


def _request(method: str, path: str, timeout: int = 30, json: dict | None = None) -> dict:
    url = _api_base().rstrip("/") + path
    try:
        resp = requests.request(method, url, json=json, timeout=timeout)
    except Exception as exc:
        raise ICloudHmeError(f"icloud-hme 服务请求失败: {exc}") from exc
    try:
        data = resp.json()
    except Exception:
        raise ICloudHmeError(f"icloud-hme 响应非 JSON: HTTP {resp.status_code} {resp.text[:200]}")
    if not data.get("success"):
        raise ICloudHmeError(f"icloud-hme 错误: {data.get('message') or data.get('error') or resp.text[:200]}")
    return data.get("data") or {}


def _find_account_for_email(email: str) -> dict | None:
    """在 icloud-hme 服务的账号里找一个能接收该别名的账号（用 /api/accounts 列表匹配 icloud_email）。"""
    data = _request("GET", "/api/accounts")
    accts = data if isinstance(data, list) else data.get("accounts") or data.get("data") or []
    if not isinstance(accts, list):
        accts = []
    # 优先匹配 icloud_email 或 real_email 与别名同主域
    for a in accts:
        icloud_email = str(a.get("icloud_email") or "").lower()
        if icloud_email and email.lower().split("@")[0] == icloud_email.split("@")[0]:
            return a
    # 兜底：用第一个 active 账号
    for a in accts:
        if a.get("status") == "active":
            return a
    return accts[0] if accts else None


def pick_account() -> ICloudHmeEmailAccount:
    """生成一个新 HME 别名，返回其邮箱地址。"""
    data = _request("GET", "/api/accounts")
    accts = data if isinstance(data, list) else data.get("accounts") or data.get("data") or []
    if not isinstance(accts, list) or not accts:
        raise ICloudHmeError("icloud-hme 没有可用账号，请先在 icloud-hme 添加 iCloud 账号（Cookie 或 SRP 登录）")
    account = next((a for a in accts if a.get("status") == "active"), accts[0])
    account_id = account.get("id") or ""

    created = _request("POST", "/api/create", json={"account_id": account_id, "label": "gpt-register"})
    email = created.get("email")
    if not email:
        raise ICloudHmeError(f"icloud-hme 创建别名未返回 email: {created}")
    acc = ICloudHmeEmailAccount(email=email, account_id=account_id, label="gpt-register")
    _CONTEXT_CACHE[email] = acc
    logger.info(f"[ICloudHME] 已创建别名: {email} (account={account_id})")
    return acc


def get_account_context(email: str) -> ICloudHmeEmailAccount | None:
    """判断邮箱是否属于本池（进程内缓存 + 服务端账号匹配）。"""
    if email in _CONTEXT_CACHE:
        return _CONTEXT_CACHE[email]
    # 仅当该邮箱看起来是 @icloud.com 别名且服务可用时才视为本池
    if "@icloud.com" not in email.lower():
        return None
    try:
        account = _find_account_for_email(email)
    except Exception:
        return None
    if account is None:
        return None
    acc = ICloudHmeEmailAccount(email=email, account_id=account.get("id") or "")
    _CONTEXT_CACHE[email] = acc
    return acc


def _extract_otp(text: str) -> str | None:
    """从邮件内容/主题提取 6 位验证码。"""
    if not text:
        return None
    # 优先 6 位数字（ChatGPT OTP 格式）
    m = re.search(r"\b(\d{6})\b", text)
    if m:
        return m.group(1)
    # 兜底：验证码关键词附近的数字
    m = re.search(r"(?:code|验证码|código|コード)\D{0,12}?(\d{4,8})", text, re.I)
    if m:
        return m.group(1)
    return None


def _forward_target_email(email: str, account_id: str) -> str:
    """找到该 HME 别名所属账号的转发目标(real_email),即验证码邮件实际落点。

    iCloud HME 别名收到的邮件会转发到账号的 real_email(Apple ID 邮箱,
    例如 chenming2002@outlook.com),不会留在 icloud 收件箱。取码必须从
    转发目标读,而不是 icloud 收件箱。
    """
    try:
        data = _request("GET", "/api/accounts")
        accts = data if isinstance(data, list) else data.get("accounts") or data.get("data") or []
        if not isinstance(accts, list):
            return ""
        for a in accts:
            if a.get("id") == account_id:
                return str(a.get("real_email") or "").strip()
            icloud_email = str(a.get("icloud_email") or "").lower()
            if icloud_email and email.lower().split("@")[0] == icloud_email.split("@")[0]:
                return str(a.get("real_email") or "").strip()
    except Exception:
        pass
    return ""


def _fetch_otp_from_forward_target(email: str, account_id: str, after_ts: float) -> str | None:
    """从转发目标(Outlook real_email)读取 OTP。若转发目标在 Outlook 池中则委托 outlook_client。

    返回 OTP 或 None(转发目标不在池 / 读取失败 / 无验证码)。
    """
    target = _forward_target_email(email, account_id)
    if not target:
        return None
    try:
        from core.outlook_client import get_account_context as outlook_ctx, fetch_latest_otp as outlook_fetch_otp
        ctx = outlook_ctx(target)
        if ctx is None:
            logger.debug(f"[ICloudHME] 转发目标 {target} 不在 Outlook 池,跳过")
            return None
        logger.info(f"[ICloudHME] {email} 转发到 {target},委托 outlook_client 取码")
        # iCloud 转发目标：绕开远端 session，强制本地 IMAP/Graph 直连（稳定 microsoftonline token）
        return outlook_fetch_otp(
            target,
            after_ts=after_ts,
            max_wait=_email_cfg.OTP_MAX_WAIT,
            poll_interval=_email_cfg.OTP_POLL_INTERVAL,
            settle_seconds=getattr(_email_cfg, "OTP_SETTLE_SECONDS", 5),
            force_direct=True,
        )
    except Exception as exc:
        logger.warning(f"[ICloudHME] 从转发目标 {target} 取码失败: {type(exc).__name__}: {exc}")
        return None


def fetch_latest_otp(
    email: str,
    after_ts: float | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
    settle_seconds: int | None = None,
) -> str:
    """轮询取验证码。

    优先从该别名的转发目标(real_email,通常是 Outlook 号)读取——HME 别名邮件
    会转发到 real_email 而非留在 icloud 收件箱。转发目标不可用时回退读 icloud 收件箱。
    """
    account = get_account_context(email)
    if account is None:
        raise ICloudHmeError(f"该邮箱不是 iCloud HME 别名: {email}")

    # 优先：从转发目标取码
    after = after_ts or 0.0
    try:
        otp = _fetch_otp_from_forward_target(email, account.account_id, after)
        if otp:
            logger.info(f"[ICloudHME] 从转发目标取到验证码: {otp}")
            return otp
        logger.info(f"[ICloudHME] 转发目标未取到验证码,回退 icloud 收件箱: {email}")
    except Exception as exc:
        logger.warning(f"[ICloudHME] 转发目标取码异常,回退 icloud 收件箱: {type(exc).__name__}: {exc}")

    deadline = time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)
    interval = poll_interval or _email_cfg.OTP_POLL_INTERVAL
    settle = settle_seconds if settle_seconds is not None else _email_cfg.OTP_SETTLE_SECONDS

    last_error = ""
    best_otp: str | None = None
    best_seen_at: float = 0.0
    settle_until: float | None = None

    while time.time() < deadline:
        try:
            data = _request("GET", f"/api/inbox?account_id={account.account_id}&alias={email}&limit=10&days=1")
            messages = data.get("messages") or []
            # 找最新一封含验证码的
            for msg in reversed(messages):
                subject = str(msg.get("subject") or "")
                preview = str(msg.get("preview") or "")
                text = f"{subject}\n{preview}"
                code = _extract_otp(text)
                if code:
                    now = time.time()
                    if code != best_otp:
                        best_otp = code
                        best_seen_at = now
                        settle_until = now + settle
                        logger.info(f"[ICloudHME] 发现验证码 {code}（{email}）")
                    else:
                        settle_until = now + settle
                    break
        except ICloudHmeError as exc:
            last_error = str(exc)
            logger.debug(f"[ICloudHME] 轮询失败: {exc}")

        if best_otp and settle_until and time.time() >= settle_until:
            logger.info(f"[ICloudHME] 验证码稳定返回: {best_otp}")
            return best_otp
        time.sleep(interval)

    if best_otp:
        return best_otp
    raise ICloudHmeError(f"等待验证码超时（>{deadline - time.time() + (max_wait or _email_cfg.OTP_MAX_WAIT)}s）: {email} {last_error}")


def release_account(email: str, status: str = "available", note: str | None = None) -> None:
    """HME 别名可复用，无需真正回收；仅清理进程内缓存。"""
    _CONTEXT_CACHE.pop(email, None)
    logger.info(f"[ICloudHME] 释放（复用）别名: {email}")
