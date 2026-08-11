# -*- coding: utf-8 -*-
"""
结合查活结果 + 邮箱邮件内容,判断账号是"封号"还是"RT过期/踢下线"。

核心函数:
    detect_account_status(email, live_result) -> dict
        返回 {verdict, reason} 其中 verdict ∈ {'live','banned','rt_expired','unknown'}

判定分两池(Outlook 池号 / iCloud HME 别名),因为邮件读取路径不同。
"""
import json
import logging
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

# OpenAI deactivate / 封号邮件关键词(subject 匹配)
_DEACTIVATE_SUBJECT_KEYWORDS = [
    "deactivated", "deleted", "banned", "suspended",
    "account deactivation", "account deletion",
    "your account has been", "account has been deactivated",
    "account has been deleted", "account was deactivated",
    "account was deleted", "account is no longer active",
    "termination of your account", "account suspended",
]


def _looks_like_deactivate_email(msg: dict) -> bool:
    """判断一条邮件消息是否疑似 OpenAI 封号/停用通知。"""
    subject = str(msg.get("subject") or "").lower()
    for kw in _DEACTIVATE_SUBJECT_KEYWORDS:
        if kw in subject:
            return True
    # 也检查发件人
    from_addr = str(msg.get("fromEmail") or msg.get("from") or "").lower()
    if "openai" in from_addr and any(k in subject for k in ["deacti", "delet", "ban", "suspend", "close"]):
        return True
    return False


def _read_mailbox_inbox(email: str, max_mails: int = 10, filter_to: str | None = None) -> list[dict]:
    """读一个 Outlook 邮箱的收件箱,返回最近 max_mails 封邮件(含 subject/from/to)。

    走本地 Graph 直连(保证含 to 字段,供 HME 池按别名过滤)。
    若该邮箱不在 Outlook 池(read 失败),返回空列表。
    """
    try:
        from core.outlook_client import get_account_context, _fetch_via_graph_direct
        ctx = get_account_context(email)
        if ctx is None:
            logger.debug("[DeadDetect] %s 不在 Outlook 池,无法读邮件", email)
            return []
        msgs = _fetch_via_graph_direct(ctx)
        if not msgs:
            return []
        # 过滤收件人(若指定)
        if filter_to:
            filter_lower = filter_to.lower()
            filtered = []
            for m in msgs:
                to_list = m.get("to") or []
                # to 可能是字符串(IMAP)或数组(Graph/REST)
                if isinstance(to_list, str):
                    if filter_lower in to_list.lower():
                        filtered.append(m)
                elif isinstance(to_list, list):
                    for t in to_list:
                        addr = (t.get("address") or "").lower() if isinstance(t, dict) else str(t).lower()
                        if filter_lower in addr:
                            filtered.append(m)
                            break
            return filtered[:max_mails]
        return msgs[:max_mails]
    except Exception as exc:
        logger.debug("[DeadDetect] 读 %s 收件箱失败: %s", email, exc)
        return []


def detect_account_status(email: str, live_result: dict) -> dict:
    """判断该邮箱的账号是正常、封号、还是 RT 过期。

    Args:
        email: 邮箱地址
        live_result: check_account_liveness() 返回的字典

    Returns:
        {verdict: 'live'|'banned'|'rt_expired'|'unknown',
         reason: str,
         checked: str}  # 判定依据
    """
    status = str(live_result.get("status") or "")
    error = str(live_result.get("error") or "")
    ok = bool(live_result.get("ok"))

    # 1. 明确正常
    if ok or status == "live":
        return {"verdict": "live", "reason": "查活正常", "checked": "live"}

    # 2. 明确封号(查活直接返回 deactivated / ban / delete)
    if status == "deactivated" or any(k in error.lower() for k in ["banned", "deleted", "deactivated"]):
        return {"verdict": "banned", "reason": error or "account_deactivated", "checked": "live"}

    # 3. 查活 failed 或其它异常——需要邮箱判定
    # 先确定邮箱来源
    try:
        from core.email_provider import resolve_email_source
        source = resolve_email_source(email)
    except Exception:
        source = ""

    reason = error or "查活异常"

    if source == "outlook":
        # Outlook 池:读该号自己的收件箱,找 deactivate 邮件
        msgs = _read_mailbox_inbox(email, max_mails=10)
        if msgs is None:
            return {"verdict": "unknown", "reason": f"{reason};无法读取邮箱邮件", "checked": "mailbox_unreachable"}
        deactivate_msgs = [m for m in msgs if _looks_like_deactivate_email(m)]
        if deactivate_msgs:
            return {"verdict": "banned", "reason": f"邮箱确认:收到 OpenAI 停用通知({deactivate_msgs[0].get('subject','')})", "checked": "mailbox"}
        return {"verdict": "rt_expired", "reason": f"{reason};邮箱未发现停用通知,可重上号", "checked": "mailbox"}

    elif source == "icloud_hme":
        # iCloud HME 别名:读转发目标收件箱,过滤 to=该别名
        from core.icloud_hme_client import get_account_context as hme_ctx, _forward_target_email
        account = hme_ctx(email)
        if account is None:
            return {"verdict": "unknown", "reason": f"{reason};无法定位 HME 别名账号", "checked": "forward_target_unknown"}
        target = _forward_target_email(email, account.account_id)
        if not target:
            return {"verdict": "unknown", "reason": f"{reason};无法获取 HME 转发目标(real_email)", "checked": "forward_target_unknown"}
        # 读转发目标收件箱,过滤 to=该别名
        msgs = _read_mailbox_inbox(target, max_mails=20, filter_to=email)
        if msgs is None:
            return {"verdict": "unknown", "reason": f"{reason};无法读取转发目标({target})邮件", "checked": "mailbox_unreachable"}
        deactivate_msgs = [m for m in msgs if _looks_like_deactivate_email(m)]
        if deactivate_msgs:
            return {"verdict": "banned", "reason": f"邮箱确认:收到 OpenAI 停用通知(to={email},{deactivate_msgs[0].get('subject','')})", "checked": "mailbox"}
        return {"verdict": "rt_expired", "reason": f"{reason};转发目标邮箱未发现停用通知,可重上号", "checked": "mailbox"}

    else:
        # 未知来源:无法读邮箱,保守处理
        return {"verdict": "unknown", "reason": f"{reason};邮箱来源({source})不支持邮件判定", "checked": "unsupported_source"}