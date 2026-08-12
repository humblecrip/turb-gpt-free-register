# -*- coding: utf-8 -*-
"""
CPA 失效号扫描 + 自动重新上号（re-auth）编排模块。

背景：CPA 侧 codex free 凭证会因 401 / disabled / unavailable / 高失败率失效。
本模块提供：
    1. scan_cpa_dead_accounts()      —— 拉 auth-files，筛出失效号（只读）
    2. delete_cpa_auth_file(name)    —— 删除 CPA 侧失效凭证（DELETE /v0/management/auth-files）
    3. is_email_reauthable(email)    —— 该邮箱在本地邮箱池能否解析（能否取 OTP 重新授权）
    4. run_reauth_pipeline(...)      —— 对每个失效号：先删 CPA 凭证 → 复用原邮箱补跑 OAuth
                                       （run_codex_oauth(force=True)），新凭证经 CPA callback 自动回传。

关键事实（探测确认，2026-08-10）：
    - CPA 删除接口：DELETE /v0/management/auth-files?name=<列表返回的完整name>
      返回 {"status":"ok"}。⚠️ 必须用列表返回的完整 name（如 codex-{email}-free.json），
      用带 hash 的旧名（codex-xxxx-{email}-free.json）删不掉。
    - 重新授权的凭证回传是天然闭环：CPA 模式下 _run_roxy_codex_oauth_once 成功路径
      调 _submit_cpa_callback，CPA 自己完成 code→token 交换并存回 auth-files，无需额外上传。
    - 补跑复用 codex_retry_service.run_worker(email)：热加载配置、独立日志、回写
      db.update_account_codex_status。
    - OTP 分派自动：email_provider.wait_for_otp(email) 按 resolve_email_source(email)
      自动走 Outlook（邮箱池 refresh_token）或 icloud_hme 等，补跑时不传 otp_provider 即可。

本模块只 import core.codex_oauth 的既有函数，不重复造轮子。
"""
import json
import logging
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Callable

from core import codex_oauth as proto
from core import codex_retry_service

logger = logging.getLogger(__name__)


# ============================================================
# 失效判定 / 扫描
# ============================================================

def _is_dead(item: dict, failed_threshold: int = 20) -> bool:
    """一个 auth-file 记录是否算失效号（基于元数据字段）。

    失效 = 显式禁用 / 状态 error / unavailable / 高失败率。
    active 且失败率正常的绝不判失效（避免误删活跃号）。
    注意：CPA 元数据常与实际 401 不同步，active 号也可能真 401，
    需配合 _is_http_401（实际探测）一起判定。
    """
    status = str(item.get("status") or "").strip().lower()
    disabled = bool(item.get("disabled"))
    unavailable = bool(item.get("unavailable"))
    failed = int(item.get("failed") or 0)
    if disabled or status in ("disabled", "error", "unavailable"):
        return True
    if unavailable:
        return True
    if failed_threshold > 0 and failed >= failed_threshold:
        return True
    return False


def parse_cpa_error_type(item: dict) -> str:
    """解析 CPA auth-file 的 status_message.error.type 为归一化错误类型。

    归一化：usage_limit_reached → usage_limit；含 invalid_api_key /
    authentication_error / 401 → unauthorized；其他 → 原样小写；
    status_message 缺失或解析失败 → 空串（不抛异常）。
    """
    raw = (item or {}).get("status_message")
    if not raw:
        return ""
    if isinstance(raw, dict):
        data = raw  # 部分接口直接返回已解析对象，同样兼容
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return ""
    etype = str(err.get("type") or "").strip().lower()
    if not etype:
        return ""
    if etype == "usage_limit_reached":
        return "usage_limit"
    if "invalid_api_key" in etype or "authentication_error" in etype or "401" in etype:
        return "unauthorized"
    return etype


def cpa_error_message(item: dict, max_len: int = 200) -> str:
    """取 status_message.error.message，截断到 max_len；缺失/解析失败返回空串。"""
    raw = (item or {}).get("status_message")
    if not raw:
        return ""
    if isinstance(raw, dict):
        data = raw  # 部分接口直接返回已解析对象，同样兼容
    else:
        try:
            data = json.loads(raw)
        except (TypeError, ValueError):
            return ""
    err = data.get("error") if isinstance(data, dict) else None
    if not isinstance(err, dict):
        return ""
    msg = str(err.get("message") or "").strip()
    if not msg:
        return ""
    return msg if len(msg) <= max_len else msg[:max_len] + "..."


def _is_http_401(item: dict, *, timeout: float = 15, max_attempts: int = 1) -> bool:
    """下载 auth-file 的 access_token，用 chatgpt_plan.check_account_plan 实际探测是否 401。

    这是 CPAView 判断"接口返回 401"的同类逻辑：
    auth-files 元数据的 id_token 里没有 access_token，只有下载完整文件才有。
    """
    name = str(item.get("name") or "").strip()
    if not name:
        return False
    try:
        content, _, _ = proto.download_cpa_codex_auth_text(cpa_name=name)
        data = json.loads(content)
        at = str(data.get("access_token") or "").strip()
        if not at:
            return False
        from core import chatgpt_plan
        res = chatgpt_plan.check_account_plan(at, timeout=timeout, max_attempts=max_attempts)
        return int(res.get("http_status") or 0) == 401
    except Exception:
        logger.debug("[CPA][Reauth] 探测 401 失败: %s", name, exc_info=True)
        return False


def scan_cpa_dead_accounts(
    *,
    failed_threshold: int = 20,
    probe_401: bool = True,
    probe_workers: int = 4,
) -> list[dict]:
    """拉 CPA auth-files，筛出失效号（只读，不删除）。

    失效判定分两层：
      1. 元数据失效：disabled / status∈{disabled,error} / unavailable / failed>=阈值。
      2. probe_401=True 时：对元数据 active 的号下载 access_token 实际探测，
         HTTP 401 也视为失效（覆盖 CPA 元数据不同步的"假活跃"号）。
         探测是 IO 密集（下载+HTTPS），用 probe_workers 并发加快（默认 4，控制 OpenAI 限流）。

    hf.space 偶发连接重置/超时，用 proto._with_net_retry 对临时网络错误重试。
    Returns: [{name, email, status, disabled, unavailable, success, failed, reauthable, dead_by}]
        dead_by: 'meta'（元数据判定）或 '401'（实际探测）或 'both'
    """
    files = proto._with_net_retry("扫描 CPA 失效号", proto.list_cpa_codex_auth_files)
    probe_candidates = []
    for item in files:
        if not isinstance(item, dict):
            continue
        meta_dead = _is_dead(item, failed_threshold=failed_threshold)
        if meta_dead:
            item = dict(item)
            item["_dead_by"] = "meta"
        elif probe_401 and int(item.get("success") or 0) > 0:
            # 元数据 active 且至少有过成功的号才值得探测
            item = dict(item)
            item["_dead_by"] = "probe"
        else:
            continue
        probe_candidates.append(item)

    # 并发探测 probe 候选
    if probe_401:
        probe_items = [it for it in probe_candidates if it.get("_dead_by") == "probe"]
        if probe_items:
            try:
                probe_workers = max(1, min(8, int(probe_workers)))
            except (TypeError, ValueError):
                probe_workers = 4
            with ThreadPoolExecutor(max_workers=probe_workers, thread_name_prefix="cpa-probe401") as ex:
                fut_map = {ex.submit(_is_http_401, it): it for it in probe_items}
                for fut in as_completed(fut_map):
                    it = fut_map[fut]
                    try:
                        is_dead = bool(fut.result())
                    except Exception:
                        logger.debug("[CPA][Reauth] 探测线程异常", exc_info=True)
                        is_dead = False
                    if is_dead:
                        it["_dead_by"] = "401"
                    else:
                        it.pop("_dead_by", None)
            probe_candidates = [it for it in probe_candidates if it.get("_dead_by")]

    dead = []
    for item in probe_candidates:
        email = str(item.get("email") or item.get("account") or "").strip()
        dead_by = item.get("_dead_by") or "meta"
        dead.append({
            "name": str(item.get("name") or "").strip(),
            "email": email,
            "status": str(item.get("status") or "").strip(),
            "disabled": bool(item.get("disabled")),
            "unavailable": bool(item.get("unavailable")),
            "success": int(item.get("success") or 0),
            "failed": int(item.get("failed") or 0),
            "reauthable": is_email_reauthable(email),
            "dead_by": dead_by,
        })
    return dead


# ============================================================
# 删除 CPA 凭证
# ============================================================

def delete_cpa_auth_file(name: str) -> None:
    """删除 CPA 侧一条 auth-file 凭证。

    必须用 CPA 列表返回的完整 name（codex-{email}-free.json），带 hash 的旧名删不掉。
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("cpa auth-file name 为空")
    proto._cpa_request_json("DELETE", f"/v0/management/auth-files?name={proto.quote(name, safe='')}")
    logger.info("[CPA][Reauth] 已删除 CPA auth-file：%s", name)


# ============================================================
# 邮箱是否可重上号
# ============================================================

def is_email_reauthable(email: str) -> bool:
    """该邮箱在本地邮箱池能否解析出取码来源（能否收到 OTP 重新授权）。

    逐个检查具体邮箱池归属（outlook / generic_api / gptmail / mailnest /
    cloudmail / icloud_hme / cloudflare_domain）。不走 email_provider.resolve_email_source，
    因为它在邮箱不属于任何池时兜底返回第一个来源（如 outlook），会把不存在的号误判为可重上。
    """
    email = (email or "").strip().lower()
    if not email:
        return False
    try:
        from core import db
        if db.get_generic_api_email_by_email(email):
            return True
        if db.get_outlook_by_email(email):
            return True
        try:
            from core.icloud_hme_client import get_account_context as _ic
            if _ic(email):
                return True
        except Exception:
            pass
        try:
            from core.gptmail_client import get_account_context as _gm
            if _gm(email):
                return True
        except Exception:
            pass
        try:
            from core.mailnest_client import get_account_context as _mn
            if _mn(email):
                return True
        except Exception:
            pass
        try:
            from core.cloudmail_client import get_account_context as _cm
            if _cm(email):
                return True
        except Exception:
            pass
        try:
            if db._find_domain_email(db._load_domain_pool(), email):
                return True
        except Exception:
            pass
        return False
    except Exception:
        logger.debug("[CPA][Reauth] 解析邮箱来源失败: %s", email, exc_info=True)
        return False


# ============================================================
# 编排：删除 + 补跑
# ============================================================

def _run_one_reauth(
    email: str,
    *,
    delete_first: bool,
    cpa_name: str | None,
    batch_id: str,
    index: int,
    total: int,
) -> dict:
    """单个邮箱的重上号：reserve → run_worker 补跑 → 成功后删旧 CPA 凭证。

    ⚠️ 顺序至关重要：先补跑成功、再删旧凭证。若先删旧凭证而补跑失败，
    会把"还能凑合用的号"直接删没（2026-08-10 事故：Roxy 挂了，19 个号被删但 0 个重上）。

    补跑内部跑 run_codex_oauth(force=True)；CPA 模式下成功即自动把新凭证
    回传给 CPA（_submit_cpa_callback），此时旧凭证可安全删除。
    失败不阻塞整批，且保留原 CPA 凭证（不删）。
    """
    email = (email or "").strip()
    result: dict = {"email": email, "ok": False, "status": "failed", "message": ""}
    try:
        if not codex_retry_service.reserve(email):
            result["status"] = "skipped"
            result["message"] = "该账号正在补跑中，跳过"
            logger.info("[CPA][Reauth] %s 已在补跑中，跳过", email)
            return result

        try:
            label = f"{batch_id} #{index}/{total}"
            run_result = codex_retry_service.run_worker(email, batch_label=label, clear_log=True)
            result["status"] = run_result.get("status") or "failed"
            result["ok"] = bool(run_result.get("ok"))
            result["message"] = str(run_result.get("message") or run_result.get("error") or "")
            if run_result.get("file_path"):
                result["file_path"] = str(run_result.get("file_path"))
            logger.info("[CPA][Reauth] %s 补跑完成：%s %s", email, result["status"], result["message"])

            # 只在补跑成功后才删旧凭证：新凭证已回传 CPA，旧凭证是重复项可安全清理
            if result.get("ok") and delete_first and cpa_name:
                try:
                    delete_cpa_auth_file(cpa_name)
                    result["message"] = (result.get("message") or "") + "；旧凭证已删除"
                except Exception as exc:
                    logger.warning("[CPA][Reauth] %s 补跑成功但删旧凭证失败（不影响新凭证）：%s", email, exc)
        finally:
            codex_retry_service.release(email)
        return result
    except Exception as exc:
        result["status"] = "failed"
        result["message"] = f"{type(exc).__name__}: {str(exc)[:200]}"
        logger.warning("[CPA][Reauth] %s 补跑异常：%s", email, exc)
        return result


def run_reauth_pipeline(
    emails: list[str],
    *,
    delete_first: bool = True,
    workers: int = 1,
    max_total: int = 50,
    cpa_names: dict[str, str] | None = None,
    callback: Callable[[dict], None] | None = None,
) -> dict:
    """批量重新上号编排。

    Args:
        emails: 要重上号的邮箱列表（已按 reauthable 过滤后的）。
        delete_first: 补跑前是否先删 CPA 侧失效凭证。
        workers: 并发线程数，1-8。
        max_total: 单次最多处理的号数（防手滑全量打爆接码）。
        cpa_names: {email: cpa_name}，删除时用列表里的完整 name。
        callback: 每完成一个号回调(result dict)。

    Returns: {ok, started:[...], skipped:[(email,reason)], batch_id, results:[...]}
    """
    emails = [str(e or "").strip() for e in emails]
    emails = [e for e in emails if e]
    if not emails:
        return {"ok": False, "started": [], "skipped": [], "batch_id": "", "results": []}
    if len(emails) > max_total:
        emails = emails[:max_total]

    try:
        workers = max(1, min(8, int(workers)))
    except (TypeError, ValueError):
        workers = 1

    batch_id = datetime.now().strftime("%Y%m%d-%H%M%S")
    skipped: list[tuple[str, str]] = []
    selected: list[str] = []
    for email in emails:
        if not is_email_reauthable(email):
            skipped.append((email, "本地邮箱池无法解析取码，跳过"))
            continue
        if codex_retry_service.is_retrying(email):
            skipped.append((email, "正在补跑中，跳过"))
            continue
        selected.append(email)

    if not selected:
        return {"ok": False, "started": [], "skipped": skipped, "batch_id": batch_id, "results": []}

    results: list[dict] = []
    total = len(selected)
    logger.info("[CPA][Reauth] 开始批量重上号 batch=%s count=%s workers=%s delete_first=%s",
                batch_id, total, workers, delete_first)

    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix=f"cpa-reauth-{batch_id}") as ex:
        futures = {
            ex.submit(
                _run_one_reauth,
                email,
                delete_first=delete_first,
                cpa_name=(cpa_names or {}).get(email),
                batch_id=batch_id,
                index=idx,
                total=total,
            ): email
            for idx, email in enumerate(selected, 1)
        }
        for fut in as_completed(futures):
            try:
                res = fut.result()
            except Exception as exc:
                res = {"email": futures[fut], "ok": False, "status": "failed",
                       "message": f"{type(exc).__name__}: {str(exc)[:200]}"}
            results.append(res)
            if callback:
                try:
                    callback(res)
                except Exception:
                    logger.debug("[CPA][Reauth] callback 异常", exc_info=True)

    ok_count = sum(1 for r in results if r.get("ok"))
    logger.info("[CPA][Reauth] 批量重上号完成 batch=%s ok=%s/%s", batch_id, ok_count, len(results))
    return {
        "ok": True,
        "started": selected,
        "skipped": skipped,
        "batch_id": batch_id,
        "results": results,
        "ok_count": ok_count,
        "failed_count": len(results) - ok_count,
    }
