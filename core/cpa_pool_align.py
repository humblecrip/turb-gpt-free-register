# -*- coding: utf-8 -*-
"""
账号池 vs CPA auth-files 双向对齐视图（只读）。

对比本地已注册账号池（db.list_accounts(archived=False)）与 CPA 实际 codex
auth-files（codex_oauth.list_cpa_codex_auth_files()），以邮箱（小写）为匹配 key，
输出四类：
    1. 池中有 & CPA 有 & 有效   —— CPA 元数据 active 且实际 401 探测非 401
    2. 池中有 & CPA 有 & 失效   —— 复用 cpa_reauth._is_dead / _is_http_401 判定
    3. 池中有 & CPA 无          —— 本地账号池有但未上 CPA
    4. CPA 有 & 池中无          —— 仅 CPA（cpa_only，单独列出）

有效性判定 = 元数据（_is_dead）+ 实际 401 探测（_is_http_401，下载 access_token
请求 OpenAI）。探测并发由 probe_workers 控制（默认 4，max 8），与
cpa_reauth.scan_cpa_dead_accounts 同源，避免 OpenAI 限流。
"""
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from core import codex_oauth as proto
from core import cpa_reauth

logger = logging.getLogger(__name__)


def _cpa_email(item: dict) -> str:
    """取 CPA auth-file 的邮箱 key（小写）；列表字段缺失时从文件名兜底解析。"""
    email = str(item.get("email") or item.get("account") or "").strip().lower()
    if email:
        return email
    name = str(item.get("name") or "").strip().lower()
    if name.startswith("codex-") and name.endswith("-free.json"):
        return name[len("codex-"):-len("-free.json")].strip()
    return ""


def _match_cpa_item(files: list[dict], email: str) -> dict | None:
    """按邮箱匹配一条 CPA codex auth 文件（复用 find_cpa_codex_auth_file 的 email 思路）。

    优先精确 email 相等；退化到邮箱包含在文件名里（兼容列表未返回 email 字段）。
    """
    email = (email or "").strip().lower()
    if not email:
        return None
    for item in files:
        if _cpa_email(item) == email:
            return item
    for item in files:
        name = str(item.get("name") or "").lower()
        if email in name:
            return item
    return None


def align_account_pool_vs_cpa(
    *,
    failed_threshold: int = 20,
    probe_401: bool = True,
    probe_workers: int = 4,
) -> dict:
    """对齐本地账号池与 CPA codex auth-files（只读，不删除）。

    Args:
        failed_threshold: 元数据 failed 字段的失效阈值（复用 _is_dead）。
        probe_401: 对元数据 active 的号是否实际探测 401。
        probe_workers: 探测并发（默认 4，max 8）。

    Returns:
        {
          ok: True,
          summary: { pool_total, in_cpa, cpa_valid, cpa_dead, not_in_cpa, cpa_only },
          accounts: [ {email, codex_status, in_cpa, cpa_valid, cpa_name, cpa_status, dead_by, note} ],
          cpa_only: [ {name, email, status} ],
        }
        accounts 数组与账号池顺序一致；dead_by ∈ {'meta','401',''}。
    """
    from core import db

    pool = db.list_accounts(archived=False) or []
    # list_cpa_codex_auth_files 已保证 dict；这里再做一次归一，防御 CPA 响应含非 dict 项
    files = [
        f for f in (proto._with_net_retry("CPA 对齐拉取 auth-files", proto.list_cpa_codex_auth_files) or [])
        if isinstance(f, dict)
    ]

    accounts: list[dict] = []
    pool_emails: set[str] = set()
    matched_names: set[str] = set()
    in_cpa = 0
    for acc in pool:
        email = str(acc.get("email") or "").strip()
        email_l = email.lower()
        item = _match_cpa_item(files, email_l) if email_l else None
        if item:
            in_cpa += 1
            matched_names.add(str(item.get("name") or "").strip())
        row = {
            "email": email,
            "codex_status": str(acc.get("codex_status") or "").strip(),
            "in_cpa": bool(item),
            "cpa_valid": False,
            "cpa_name": str((item or {}).get("name") or "").strip() if item else "",
            "cpa_status": str((item or {}).get("status") or "").strip() if item else "",
            "dead_by": "",
            "note": "",
        }
        if item:
            if cpa_reauth._is_dead(item, failed_threshold=failed_threshold):
                row["dead_by"] = "meta"
                row["note"] = "CPA 元数据失效（disabled/error/unavailable/高失败）"
            elif probe_401:
                row["dead_by"] = "probe"  # 待并发探测
            else:
                row["cpa_valid"] = True
                row["note"] = "CPA 元数据正常"
        else:
            row["note"] = "CPA auth-files 中没有该邮箱的 codex 凭证"
        pool_emails.add(email_l)
        accounts.append(row)

    # 并发探测元数据 active 的号：下载 access_token 实际请求 OpenAI 判定 401
    if probe_401:
        name_to_item = {str(f.get("name") or "").strip(): f for f in files if isinstance(f, dict)}
        probe_targets = [a for a in accounts if a["in_cpa"] and a["dead_by"] == "probe"]
        if probe_targets:
            try:
                workers = max(1, min(8, int(probe_workers)))
            except (TypeError, ValueError):
                workers = 4
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="cpa-align-probe") as ex:
                fut_map = {
                    ex.submit(cpa_reauth._is_http_401, name_to_item.get(a["cpa_name"]) or {"name": a["cpa_name"]}): a
                    for a in probe_targets
                }
                for fut in as_completed(fut_map):
                    a = fut_map[fut]
                    try:
                        is_401 = bool(fut.result())
                    except Exception:
                        logger.debug("[CPA][Align] 探测线程异常", exc_info=True)
                        is_401 = False
                    if is_401:
                        a["dead_by"] = "401"
                        a["note"] = "实际请求 OpenAI 返回 401"
                    else:
                        a["dead_by"] = ""
                        a["cpa_valid"] = True
                        a["note"] = "CPA 元数据正常且实际探测非 401"

    # CPA 有但账号池无：按 email 精确匹配排除（避免子串误判），文件名已匹配的也不再列出
    cpa_only: list[dict] = []
    for item in files:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        email = _cpa_email(item)
        if email and email in pool_emails:
            continue
        if name in matched_names:
            continue
        cpa_only.append({
            "name": name,
            "email": email,
            "status": str(item.get("status") or "").strip(),
        })

    cpa_valid = sum(1 for a in accounts if a["cpa_valid"])
    cpa_dead = sum(1 for a in accounts if a["in_cpa"] and not a["cpa_valid"])
    not_in_cpa = sum(1 for a in accounts if not a["in_cpa"])
    summary = {
        "pool_total": len(accounts),
        "in_cpa": in_cpa,
        "cpa_valid": cpa_valid,
        "cpa_dead": cpa_dead,
        "not_in_cpa": not_in_cpa,
        "cpa_only": len(cpa_only),
    }
    logger.info(
        "[CPA][Align] 对齐完成：pool=%s in_cpa=%s valid=%s dead=%s not_in_cpa=%s cpa_only=%s",
        summary["pool_total"], summary["in_cpa"], summary["cpa_valid"],
        summary["cpa_dead"], summary["not_in_cpa"], summary["cpa_only"],
    )
    return {"ok": True, "summary": summary, "accounts": accounts, "cpa_only": cpa_only}
