# -*- coding: utf-8 -*-
"""
iCloud 工作流编排服务：领别名 → 注册 → 接码 → Codex 补跑 → 自动上传 CPA。

设计：
    - 每个号一个 job，状态机保存在内存 dict（WebUI 轮询读取），不做 DB 持久化。
    - 后台 dispatcher 线程 + ThreadPoolExecutor：并发受 `workers` 控制，
      dispatcher 只在有并发余量时把排队的 job_id 交给 executor，从而支持
      暂停（不再提交新 job）与停止（标记剩余排队 job）。
    - 每个 job 步骤：
        step 0 领别名   icloud_hme_client.pick_account()
        step 1 注册     main.run_registration(email, name, birthday)
        step 2 接码     （codex_retry_service.run_worker 内部使用 SMS，国家在 start 时覆盖）
        step 3 补跑     codex_retry_service.run_worker(email)
        step 4 上传CPA  codex_oauth.upload_cpa_auth_file(email)（auto_upload=True 时）
    - 复用现有能力，不重复造轮子：
        core.icloud_hme_client.pick_account / fetch_latest_otp
        main.run_registration（OTP 自动取码，email_provider 自动解析 icloud_hme 源）
        core.codex_retry_service.run_worker（内部含 CPA auth 上传）
        core.codex_oauth.upload_cpa_auth_file

注意：
    - start(country=...) 会临时覆盖 config.codex.SMS_COUNTRY，并把这个国家前置到
      接码国家队列头（sms_provider 全局 prefer，优先级最高），批次结束/停止时恢复；
      其余队列按配置 SMS_COUNTRY_SORT 决定（manual / auto_price / auto_success）。
    - 单号重试：failed/stopped/paused 的 job 重新入队，已领别名/已注册的 job 从
      断点继续（不重复领别名/注册）。
"""
import logging
import queue as queue_mod
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

logger = logging.getLogger(__name__)

STEP_LABELS = ["领别名", "注册", "接码", "补跑", "上传CPA"]

# job 状态（与前端展示一致）
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_PAUSED = "paused"
STATUS_STOPPED = "stopped"
STATUS_DONE = "done"
STATUS_FAILED = "failed"

_MAX_COUNT = 200
_MAX_WORKERS = 16
_MIN_WORKERS = 1


class _JobStop(Exception):
    """本 job 被用户停止。"""


class ICloudWorkflowError(RuntimeError):
    """工作流业务错误（会落到 job.error）。"""


# ------------------------------------------------------------
# 进程内状态
# ------------------------------------------------------------
_STATE_LOCK = threading.RLock()
_STATE: dict = {
    "running": False,
    "paused": False,
    "stop_requested": False,
    "batch_id": None,
    "config": {},          # {"count","country","workers","auto_upload"}
    "jobs": {},            # job_id -> job dict
    "queue": queue_mod.Queue(),  # 排队 job_id
    "executor": None,
    "dispatcher_thread": None,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
}

# SMS 国家临时覆盖（start 时设置，批次结束/停止时恢复）
_ORIG_SMS_COUNTRY: str | None = None

# 进程内"今天已创建别名"兜底计数（/api/aliases 不可用时使用）
_LOCAL_TODAY: dict = {"date": "", "count": 0}


def _now() -> str:
    return datetime.now().strftime("%H:%M:%S")


def _new_job() -> dict:
    job_id = uuid.uuid4().hex[:12]
    return {
        "job_id": job_id,
        "email": None,
        "account_id": None,
        "status": STATUS_QUEUED,
        "step": 0,
        "step_label": STEP_LABELS[0],
        "logs": [],
        "error": None,
        "created_at": _now(),
        "updated_at": _now(),
        "finished_at": None,
        "stop_requested": False,
    }


def _get_job(job_id: str) -> dict | None:
    with _STATE_LOCK:
        return _STATE["jobs"].get(str(job_id))


def _update_job(job_id: str, **changes) -> None:
    with _STATE_LOCK:
        job = _STATE["jobs"].get(str(job_id))
        if job is None:
            return
        job.update(changes)
        job["updated_at"] = _now()


def _log_job(job_id: str, message: str, level: str = "info") -> None:
    entry = {"ts": _now(), "level": level, "message": message}
    with _STATE_LOCK:
        job = _STATE["jobs"].get(str(job_id))
        if job is None:
            return
        job["logs"].append(entry)
        job["updated_at"] = _now()
    if level == "error":
        logger.error("[iCloud工作流][%s] %s", job_id, message)
    elif level == "warn":
        logger.warning("[iCloud工作流][%s] %s", job_id, message)
    else:
        logger.info("[iCloud工作流][%s] %s", job_id, message)


def _set_step(job_id: str, step: int) -> None:
    step = max(0, min(len(STEP_LABELS) - 1, int(step or 0)))
    _update_job(job_id, step=step, step_label=STEP_LABELS[step])


def _check_job_stop(job_id: str) -> None:
    """检查全局停止或本 job 停止请求。"""
    with _STATE_LOCK:
        global_stop = _STATE.get("stop_requested") or False
    if global_stop:
        raise _JobStop("批处理已停止")
    job = _get_job(job_id)
    if job and job.get("stop_requested"):
        raise _JobStop("用户停止该号")


def _count_running() -> int:
    with _STATE_LOCK:
        return sum(1 for j in _STATE["jobs"].values() if j.get("status") == STATUS_RUNNING)


def _mark_queued_paused() -> None:
    """暂停时把仍排队的 job 标记为 paused（展示用；重新开始会恢复为 queued）。"""
    with _STATE_LOCK:
        for job in _STATE["jobs"].values():
            if job.get("status") == STATUS_QUEUED:
                job["status"] = STATUS_PAUSED
                job["updated_at"] = _now()


def _bump_local_today() -> None:
    today = datetime.now().strftime("%Y-%m-%d")
    with _STATE_LOCK:
        if _LOCAL_TODAY["date"] != today:
            _LOCAL_TODAY["date"] = today
            _LOCAL_TODAY["count"] = 0
        _LOCAL_TODAY["count"] += 1


def local_today_created() -> int:
    today = datetime.now().strftime("%Y-%m-%d")
    with _STATE_LOCK:
        if _LOCAL_TODAY["date"] != today:
            return 0
        return int(_LOCAL_TODAY["count"] or 0)


# ------------------------------------------------------------
# SMS 国家临时覆盖
# ------------------------------------------------------------
def _apply_sms_country(country: str) -> None:
    global _ORIG_SMS_COUNTRY
    country = str(country or "").strip()
    if not country:
        return
    try:
        from config import codex as _cfg
        from core import sms_provider
        _ORIG_SMS_COUNTRY = getattr(_cfg, "SMS_COUNTRY", "") or ""
        _cfg.SMS_COUNTRY = country
        sms_provider.set_country_prefer(country)
        logger.info("[iCloud工作流] 临时覆盖 SMS_COUNTRY=%s（原=%s），并前置到国家队列", country, _ORIG_SMS_COUNTRY or "默认")
    except Exception as exc:
        logger.warning("[iCloud工作流] SMS_COUNTRY 覆盖失败: %s", exc)


def _restore_sms_country() -> None:
    global _ORIG_SMS_COUNTRY
    if _ORIG_SMS_COUNTRY is None:
        return
    try:
        from config import codex as _cfg
        from core import sms_provider
        _cfg.SMS_COUNTRY = _ORIG_SMS_COUNTRY
        sms_provider.set_country_prefer(None)
        logger.info("[iCloud工作流] 已恢复 SMS_COUNTRY=%s", _ORIG_SMS_COUNTRY or "默认")
    except Exception:
        pass
    finally:
        _ORIG_SMS_COUNTRY = None


# ------------------------------------------------------------
# 单 job 执行
# ------------------------------------------------------------
def _run_job(job_id: str) -> None:
    job = _get_job(job_id)
    if job is None:
        return
    with _STATE_LOCK:
        if job.get("status") == STATUS_STOPPED:
            return
    _update_job(job_id, status=STATUS_RUNNING, error=None, stop_requested=False)
    _log_job(job_id, "开始执行")
    try:
        email = job.get("email") or ""
        account_id = job.get("account_id")

        # ---------------- step 0: 领别名 ----------------
        if not email:
            _set_step(job_id, 0)
            _check_job_stop(job_id)
            _log_job(job_id, "正在领取 iCloud 别名…")
            from core import icloud_hme_client
            acc = icloud_hme_client.pick_account()
            email = acc.email
            _update_job(job_id, email=email)
            _bump_local_today()
            _log_job(job_id, f"已领取别名: {email}")

        # ---------------- step 1: 注册 ----------------
        if not account_id:
            _set_step(job_id, 1)
            _check_job_stop(job_id)
            # 协议驱动注册在 USE_EMAIL_SERVICE=False 时会 input() 阻塞后台线程，
            # 这里直接给出可操作的错误，避免整个 worker 卡死。
            try:
                from config import email as _email_cfg
                use_email_service = bool(getattr(_email_cfg, "USE_EMAIL_SERVICE", True))
            except Exception:
                use_email_service = True
            if not use_email_service:
                raise ICloudWorkflowError(
                    "USE_EMAIL_SERVICE=False：后台工作流无法手动输入验证码，"
                    "请先在「配置 → 邮箱 / OTP」开启自动取件（或改用 roxy/cloak/browser_use 驱动）"
                )
            _log_job(job_id, "正在注册（OTP 自动取码）…")
            from main import run_registration
            from core.name_samples import random_display_name
            from core.profile_utils import generate_random_birthday

            result = run_registration(
                email=email,
                name=random_display_name(),
                birthday=generate_random_birthday(),
            )
            if not (isinstance(result, dict) and result.get("success")):
                err = str((result or {}).get("error") or "注册失败")[:500]
                raise ICloudWorkflowError(err)
            account_id = result.get("account_id")
            _update_job(job_id, account_id=account_id)
            reg_codex_ok = bool(((result.get("codex") or {}).get("ok")))
            _log_job(job_id, f"注册成功 account_id={account_id}")
            if reg_codex_ok:
                _log_job(job_id, "注册流程已顺带完成 Codex 授权，跳过补跑步骤")
        else:
            reg_codex_ok = False

        # ---------------- step 2/3: 接码 + 补跑 ----------------
        if not reg_codex_ok:
            _set_step(job_id, 2)
            _check_job_stop(job_id)
            _log_job(job_id, "准备 Codex 补跑（接码国家按批次设置）…")
            from core import codex_retry_service

            if not codex_retry_service.reserve(email):
                raise ICloudWorkflowError("该邮箱正在补跑中，请稍后重试")
            _set_step(job_id, 3)
            _log_job(job_id, "开始 Codex 补跑…")
            codex_result = codex_retry_service.run_worker(
                email,
                batch_label=f"icloud-wf-{_STATE['batch_id']}",
                clear_log=False,
            )
            if not codex_result.get("ok"):
                raise ICloudWorkflowError(
                    str(codex_result.get("message") or "Codex 补跑失败")[:500]
                )
            _log_job(job_id, "Codex 补跑成功")

        # ---------------- step 4: 上传 CPA ----------------
        auto_upload = bool(_STATE.get("config", {}).get("auto_upload", True))
        if auto_upload:
            _set_step(job_id, 4)
            _check_job_stop(job_id)
            _log_job(job_id, "正在上传 CPA auth 文件…")
            from core.codex_oauth import upload_cpa_auth_file

            try:
                upload_cpa_auth_file(email=email)
                _log_job(job_id, "CPA auth 文件已上传")
            except Exception as exc:
                _log_job(job_id, f"CPA 上传失败（不阻塞结果）: {type(exc).__name__}: {exc}", "warn")
        else:
            _log_job(job_id, "未勾选自动上传 CPA，跳过")

        _update_job(job_id, status=STATUS_DONE, finished_at=_now())
        _log_job(job_id, "工作流完成 ✔")
    except _JobStop:
        _update_job(job_id, status=STATUS_STOPPED, error="用户停止", finished_at=_now())
        _log_job(job_id, "已停止", "warn")
        _stop_codex_if_running(job_id)
    except ICloudWorkflowError as exc:
        _update_job(job_id, status=STATUS_FAILED, error=str(exc), finished_at=_now())
        _log_job(job_id, f"失败: {exc}", "error")
        _stop_codex_if_running(job_id)
    except Exception as exc:
        err = f"{type(exc).__name__}: {exc}"[:500]
        _update_job(job_id, status=STATUS_FAILED, error=err, finished_at=_now())
        _log_job(job_id, f"异常失败: {err}", "error")
        _stop_codex_if_running(job_id)


def _stop_codex_if_running(job_id: str) -> None:
    """job 中止时，若账号正在补跑 Codex，向其发送停止信号。"""
    job = _get_job(job_id)
    email = (job or {}).get("email") or ""
    if not email:
        return
    try:
        from core import codex_retry_service
        if codex_retry_service.is_retrying(email):
            codex_retry_service.request_stop(email)
    except Exception:
        pass


# ------------------------------------------------------------
# dispatcher：排队 → executor
# ------------------------------------------------------------
def _dispatcher_loop(batch_id: str) -> None:
    try:
        while True:
            with _STATE_LOCK:
                if _STATE.get("stop_requested"):
                    _STATE["queue"] = queue_mod.Queue()
                    # 剩余排队 job 标记 stopped
                    for job in _STATE["jobs"].values():
                        if job.get("status") in (STATUS_QUEUED, STATUS_PAUSED):
                            job["status"] = STATUS_STOPPED
                            job["error"] = "批处理已停止"
                            job["finished_at"] = _now()
                            job["updated_at"] = _now()
                    break
                if not _STATE.get("running"):
                    break
                paused = _STATE.get("paused") or False
                workers = int(_STATE.get("config", {}).get("workers") or 1)
                running_count = sum(1 for j in _STATE["jobs"].values() if j.get("status") == STATUS_RUNNING)

            if paused:
                _mark_queued_paused()
                time.sleep(0.5)
                continue
            if running_count >= workers:
                time.sleep(0.5)
                continue

            try:
                job_id = _STATE["queue"].get_nowait()
            except queue_mod.Empty:
                # 没有排队任务：等待，直到全部结束
                with _STATE_LOCK:
                    any_active = any(
                        j.get("status") in (STATUS_QUEUED, STATUS_RUNNING)
                        for j in _STATE["jobs"].values()
                    )
                if not any_active:
                    break
                time.sleep(0.5)
                continue

            # 提交前再检查一次停止/暂停
            with _STATE_LOCK:
                if _STATE.get("stop_requested"):
                    job = _STATE["jobs"].get(str(job_id))
                    if job:
                        job["status"] = STATUS_STOPPED
                        job["error"] = "批处理已停止"
                        job["finished_at"] = _now()
                    break
                if _STATE.get("paused"):
                    job = _STATE["jobs"].get(str(job_id))
                    if job:
                        job["status"] = STATUS_PAUSED
                    _STATE["queue"].put_nowait(job_id)  # 放回队尾
                    time.sleep(0.5)
                    continue
                executor = _STATE.get("executor")
                if executor is None:
                    break
                executor.submit(_run_job, str(job_id))
    except Exception as exc:
        logger.exception("[iCloud工作流] dispatcher 异常: %s", exc)
    finally:
        # 批次结束：若仍在运行中则标记完成状态
        with _STATE_LOCK:
            _STATE["running"] = False
            _STATE["paused"] = False
            _STATE["finished_at"] = _now()
            executor = _STATE.get("executor")
            _STATE["executor"] = None
        if executor is not None:
            try:
                executor.shutdown(wait=False, cancel_futures=False)
            except Exception:
                pass
        _restore_sms_country()
        logger.info("[iCloud工作流] 批次 %s 结束", batch_id)


# ------------------------------------------------------------
# 公共接口
# ------------------------------------------------------------
def start(
    count: int = 1,
    country: str = "",
    workers: int | None = None,
    auto_upload: bool = True,
) -> dict:
    """启动一个 iCloud 工作流批次。返回 {ok, jobs:[...]} 或错误。"""
    try:
        count = max(1, min(_MAX_COUNT, int(count or 1)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "count 必须是数字", "status": 400}
    try:
        workers = max(_MIN_WORKERS, min(_MAX_WORKERS, int(workers or 1)))
    except (TypeError, ValueError):
        return {"ok": False, "error": "workers 必须是数字", "status": 400}

    with _STATE_LOCK:
        if _STATE.get("running"):
            return {"ok": False, "error": "已有批次在运行中，请先停止", "status": 409}

        batch_id = uuid.uuid4().hex[:8]
        _STATE.update({
            "running": True,
            "paused": False,
            "stop_requested": False,
            "batch_id": batch_id,
            "config": {
                "count": count,
                "country": str(country or "").strip(),
                "workers": workers,
                "auto_upload": bool(auto_upload),
            },
            "jobs": {},
            "queue": queue_mod.Queue(),
            "executor": ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"icloud-wf-{batch_id}",
            ),
            "dispatcher_thread": None,
            "started_at": _now(),
            "finished_at": None,
            "last_error": None,
        })
        jobs = []
        for _ in range(count):
            job = _new_job()
            _STATE["jobs"][job["job_id"]] = job
            _STATE["queue"].put_nowait(job["job_id"])
            jobs.append(job)

    country = str(country or "").strip()
    if country:
        _apply_sms_country(country)

    t = threading.Thread(
        target=_dispatcher_loop,
        args=(batch_id,),
        name=f"icloud-wf-dispatcher-{batch_id}",
        daemon=True,
    )
    with _STATE_LOCK:
        _STATE["dispatcher_thread"] = t
    t.start()

    logger.info("[iCloud工作流] 批次 %s 已启动: count=%s workers=%s country=%r auto_upload=%s",
                batch_id, count, workers, country or "-", bool(auto_upload))
    return {"ok": True, "batch_id": batch_id, "jobs": jobs}


def pause() -> dict:
    """暂停：不再提交新 job；正在运行的 job 继续完成，排队 job 标记 paused。"""
    with _STATE_LOCK:
        if not _STATE.get("running"):
            return {"ok": False, "error": "没有运行中的批次", "status": 409}
        _STATE["paused"] = True
    _mark_queued_paused()
    return {"ok": True, "message": "已暂停：不再开始新的号，正在运行的号会跑完当前步骤"}


def resume() -> dict:
    """继续：排队中的 paused job 恢复为 queued，dispatcher 继续提交。"""
    with _STATE_LOCK:
        if not _STATE.get("running"):
            return {"ok": False, "error": "没有运行中的批次", "status": 409}
        _STATE["paused"] = False
        for job in _STATE["jobs"].values():
            if job.get("status") == STATUS_PAUSED:
                job["status"] = STATUS_QUEUED
                job["error"] = None
    return {"ok": True, "message": "已继续"}


def stop() -> dict:
    """停止：停止队列，排队 job 标记 stopped；运行中 job 在步骤边界停止。"""
    with _STATE_LOCK:
        if not _STATE.get("running") and not _STATE.get("jobs"):
            return {"ok": False, "error": "没有运行中的批次", "status": 409}
        _STATE["stop_requested"] = True
        _STATE["paused"] = False
        _STATE["running"] = False
        stopped_count = 0
        for job in _STATE["jobs"].values():
            if job.get("status") in (STATUS_QUEUED, STATUS_PAUSED):
                job["status"] = STATUS_STOPPED
                job["error"] = "批处理已停止"
                job["finished_at"] = _now()
                stopped_count += 1
    return {"ok": True, "message": f"已请求停止；{stopped_count} 个排队号已标记停止，运行中的号会在步骤边界停止"}


def retry(job_id: str) -> dict:
    """单号重试：failed/stopped/paused 的 job 重新入队，从断点继续。

    若批次已结束（running=False），会自动重建 dispatcher 只处理本次重试，
    避免用户在所有号跑完后无法单独补跑失败号。
    """
    job_id = str(job_id or "").strip()
    job = _get_job(job_id)
    if job is None:
        return {"ok": False, "error": "job 不存在", "status": 404}
    status = job.get("status")
    if status not in (STATUS_FAILED, STATUS_STOPPED, STATUS_PAUSED):
        return {"ok": False, "error": f"当前状态不支持重试：{status}", "status": 409}
    with _STATE_LOCK:
        _STATE["paused"] = False
        _STATE["stop_requested"] = False
        _STATE["running"] = True
        # 已领别名/已注册的 job 保留 email/account_id，从断点继续
        _update_job(job_id, status=STATUS_QUEUED, error=None, stop_requested=False, finished_at=None)
        _STATE["queue"].put_nowait(job_id)
        if _STATE.get("executor") is None:
            workers = max(_MIN_WORKERS, min(_MAX_WORKERS, int(_STATE.get("config", {}).get("workers") or 1)))
            _STATE["executor"] = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix=f"icloud-wf-retry-{_STATE.get('batch_id') or 'x'}",
            )
    _log_job(job_id, "用户请求重试，已重新入队")
    # 若 dispatcher 已退出（批次结束），重建一个只处理重试的 dispatcher
    with _STATE_LOCK:
        dispatcher = _STATE.get("dispatcher_thread")
        alive = dispatcher is not None and dispatcher.is_alive()
        batch_id = _STATE.get("batch_id") or "retry"
    if not alive:
        t = threading.Thread(
            target=_dispatcher_loop,
            args=(f"{batch_id}-retry",),
            name=f"icloud-wf-dispatcher-{batch_id}-retry",
            daemon=True,
        )
        with _STATE_LOCK:
            _STATE["dispatcher_thread"] = t
        t.start()
        logger.info("[iCloud工作流] 批次已结束，为重试重建 dispatcher: job=%s", job_id)
    return {"ok": True, "message": "已重新入队"}


def status() -> dict:
    """当前批次状态 + 各号进度 + 别名池状态。"""
    with _STATE_LOCK:
        running = bool(_STATE.get("running"))
        paused = bool(_STATE.get("paused"))
        batch_id = _STATE.get("batch_id")
        config = dict(_STATE.get("config") or {})
        jobs = [dict(j) for j in _STATE["jobs"].values()]
        started_at = _STATE.get("started_at")
        finished_at = _STATE.get("finished_at")
    counts = {s: 0 for s in (STATUS_QUEUED, STATUS_RUNNING, STATUS_PAUSED, STATUS_STOPPED, STATUS_DONE, STATUS_FAILED)}
    for job in jobs:
        counts[job.get("status")] = counts.get(job.get("status"), 0) + 1
    pool = alias_pool_status()
    return {
        "ok": True,
        "running": running,
        "paused": paused,
        "batch_id": batch_id,
        "config": config,
        "jobs": jobs,
        "counts": counts,
        "pool": pool,
        "started_at": started_at,
        "finished_at": finished_at,
    }


# ------------------------------------------------------------
# 别名池状态
# ------------------------------------------------------------
def alias_pool_status() -> dict:
    """
    读取 icloud-hme 别名池状态：
      GET /api/accounts   → 账号数 / alias_total / alias_active
      GET /api/aliases?account_id=xxx → 总别名 / 今天创建
    任一接口不可用时降级：total/remaining 用账号汇总字段；今天创建用进程内计数。
    """
    result = {
        "ok": True,
        "accounts": 0,        # icloud-hme 账号数
        "total_aliases": None,  # 总别名数
        "today_created": 0,   # 今天已创建
        "remaining": None,    # 剩余可用（alias_active 汇总）
        "source": "api",
        "error": None,
    }
    try:
        from core import icloud_hme_client

        data = icloud_hme_client._request("GET", "/api/accounts")
        accts = data if isinstance(data, list) else (data.get("accounts") or data.get("data") or [])
        if not isinstance(accts, list):
            accts = []
        result["accounts"] = len(accts)
        total = 0
        remaining = 0
        for a in accts:
            try:
                total += int(a.get("alias_total") or 0)
            except (TypeError, ValueError):
                pass
            try:
                remaining += int(a.get("alias_active") or 0)
            except (TypeError, ValueError):
                pass
        result["total_aliases"] = total
        result["remaining"] = remaining
    except Exception as exc:
        result["ok"] = False
        result["error"] = str(exc)[:300]
        result["source"] = "error"
        return result

    # 尝试按账号拉别名，统计今天创建
    today = datetime.now().strftime("%Y-%m-%d")
    today_count = 0
    got_aliases = False
    try:
        from core import icloud_hme_client
        for a in accts:
            try:
                al = icloud_hme_client._request(
                    "GET", f"/api/aliases?account_id={a.get('id')}"
                )
                aliases = al.get("aliases") if isinstance(al, dict) else None
                if not isinstance(aliases, list):
                    aliases = []
                for item in aliases:
                    created = str(item.get("created_at") or "")
                    if created[:10] == today:
                        today_count += 1
                got_aliases = True
            except Exception:
                continue
    except Exception:
        pass

    if got_aliases:
        result["today_created"] = today_count
        result["source"] = "api"
    else:
        result["today_created"] = local_today_created()
        result["source"] = "api+local"
    return result
