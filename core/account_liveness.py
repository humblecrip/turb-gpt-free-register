# -*- coding: utf-8 -*-
"""已注册账号查活：优先轻量 token 探测，失效/不确定再完整重新登录。

- light：用账号库现有 access_token 打套餐查询接口探测（不重新登录、不烧 OTP）
- full：完整重新邮箱 OTP 登录，成功拿到最新 ChatGPT accessToken 即视为正常
- auto：先 light，live/deactivated 直接返回，failed（不确定）降级 full 确认
"""
import logging
import threading
import time
from datetime import datetime
from pathlib import Path

from core import db
from core.session import BrowserSession
from core.chatgpt_plan import check_account_plan
from core.chatgpt_auth import get_providers, get_csrf_token, signin_openai
from core.openai_auth import (
    follow_authorize,
    send_email_otp,
    validate_email_otp,
    EmailOtpInvalidError,
    AccountUnusableError,
    detect_account_unusable_text,
)
from core.account_export import follow_oauth_callback, fetch_session
from core.email_provider import wait_for_otp

logger = logging.getLogger(__name__)
_LOG_DIR = Path(__file__).resolve().parent.parent / "注册日志"
_RUNNING: set[str] = set()
_RUNNING_LOCK = threading.Lock()
_VALID_MODES = {"full", "light", "auto"}

# 查活网络预检失败（403/429/代理/超时等）多为出口 IP 被 CF 标记或代理池抖动，
# 视为可换新 IP 重试；账号本身问题（废号/邮箱错误等）不重试。
_RETRYABLE_NETWORK_HINTS = (
    "403", "429", "502", "503", "504",
    "proxy", "socks", "timeout", "timed out",
    "connection", "closed", "reset",
)


def _is_retryable_network_error(exc: BaseException) -> bool:
    if isinstance(exc, AccountUnusableError):
        return False
    text = str(exc or "").lower()
    return any(h in text for h in _RETRYABLE_NETWORK_HINTS)


def _network_preflight_with_retry(email: str, proxy: str | None, max_attempts: int = 4) -> tuple[BrowserSession, str]:
    """Providers → CSRF → Signin 网络预检；失败换新 IP 重试（每轮新会话新代理）。"""
    session: BrowserSession | None = None
    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        if session is not None:
            try:
                session.session.close()
            except Exception:
                pass
        session = BrowserSession(proxy=proxy)
        logger.info(
            "[查活] 会话创建完成：proxy=%s device_id=%s（网络预检第 %s/%s 次）",
            session.proxy or "配置随机/直连", session.device_id, attempt, max_attempts,
        )
        try:
            get_providers(session)
            csrf = get_csrf_token(session)
            authorize_url = signin_openai(session, csrf, email)
            return session, authorize_url
        except Exception as exc:
            last_exc = exc
            if attempt >= max_attempts or not _is_retryable_network_error(exc):
                raise
            logger.warning(
                "[查活] 网络预检失败（%s/%s），换新 IP 重试：%s",
                attempt, max_attempts, str(exc)[:200],
            )
            time.sleep(2)
    raise RuntimeError(f"网络预检多次失败：{last_exc}")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def log_path(email: str) -> Path:
    safe = str(email or "").replace("/", "_").replace("\\", "_").replace(":", "_")
    return _LOG_DIR / f"live-check-{safe}.log"


def is_checking(email: str) -> bool:
    key = str(email or "").strip().lower()
    with _RUNNING_LOCK:
        return key in _RUNNING


def _validate_with_retry(session: BrowserSession, email: str, otp_after_ts: float, max_otp_attempts: int = 3) -> dict:
    current_otp = None
    last_exc: Exception | None = None
    for attempt in range(1, max_otp_attempts + 1):
        try:
            if current_otp is None:
                logger.info("[查活] 等待登录 OTP：%s（第 %s/%s 次）", email, attempt, max_otp_attempts)
                current_otp = wait_for_otp(email, after_ts=otp_after_ts)
            result = validate_email_otp(session, current_otp, sentinel_header=None, so_header=None)
            return result
        except EmailOtpInvalidError as exc:
            last_exc = exc
            if attempt >= max_otp_attempts:
                break
            logger.warning("[查活] OTP 无效/过期，重新发送后再取：%s", str(exc)[:180])
            send_email_otp(session)
            # 以“重新发送请求完成后”为新基准，避免刚刚失败的上一封旧码再次被 after 容忍窗口命中。
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
        except Exception as exc:
            # 提交 OTP 后的网络抖动（连接断开/超时/代理波动）：同一会话重发验证码再验证一次。
            if attempt >= max_otp_attempts or not _is_retryable_network_error(exc):
                raise
            last_exc = exc
            logger.warning("[查活] OTP 验证网络抖动，重新发送后再取（%s/%s）：%s", attempt, max_otp_attempts, str(exc)[:180])
            try:
                send_email_otp(session)
            except Exception:
                raise
            otp_after_ts = time.time()
            current_otp = None
            time.sleep(1)
    raise last_exc if last_exc else RuntimeError("OTP 验证失败")


def _make_file_handler(path: Path) -> logging.FileHandler:
    """按当前线程创建查活日志 FileHandler，只记录本线程的日志。"""
    thread_name = threading.current_thread().name
    fh = logging.FileHandler(str(path), encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    ))
    fh.addFilter(lambda record: record.threadName == thread_name)
    return fh


def _probe_liveness_light(email: str, proxy: str | None, checked_at: str) -> dict:
    """轻量 token 探测核心：取账号库 access_token，打套餐查询接口。

    返回 status：live（200）/ deactivated（401 或 token_expired）/ failed（其他）。
    不重新登录、不烧 OTP；探测走 check_account_plan（BrowserSession/curl_cffi，
    http/https/socks5h 自动适配，协议无关）。
    """
    try:
        acc = db.get_account_by_email(email)
    except Exception as exc:
        return {"ok": False, "status": "failed", "checked_at": checked_at,
                "error": f"读取账号库失败: {type(exc).__name__}: {str(exc)[:300]}"}
    if not acc:
        return {"ok": False, "status": "failed", "checked_at": checked_at,
                "error": "账号库中未找到该账号，需重新登录"}
    token = str(acc.get("access_token") or "").strip()
    if not token:
        return {"ok": False, "status": "failed", "checked_at": checked_at,
                "error": "账号库无token，需重新登录"}

    logger.info("[查活-轻量] token 探测：%s", email)
    try:
        probe = check_account_plan(token, proxy=proxy, timeout=20, max_attempts=2, retry_delay=1.0)
    except Exception as exc:
        return {"ok": False, "status": "failed", "checked_at": checked_at,
                "error": f"{type(exc).__name__}: {str(exc)[:300]}"}

    http_status = probe.get("http_status")
    error = str(probe.get("error") or "")
    if bool(probe.get("ok")) and http_status == 200:
        logger.info("[查活-轻量] 正常：%s plan=%s", email, probe.get("current_plan_type"))
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": token,
            "http_status": http_status,
            "plan_type": probe.get("current_plan_type"),
        }
    if http_status == 401 or probe.get("token_expired"):
        logger.warning("[查活-轻量] token 失效：%s", email)
        return {
            "ok": False,
            "status": "deactivated",
            "checked_at": checked_at,
            "http_status": http_status,
            "error": error or "AT已过期/失效，需重新登录",
        }
    logger.warning("[查活-轻量] 不确定：%s http_status=%s error=%s", email, http_status, error[:200])
    return {
        "ok": False,
        "status": "failed",
        "checked_at": checked_at,
        "http_status": http_status,
        "error": error or f"token 探测失败 http_status={http_status}",
    }


def check_account_liveness_light(email: str, proxy: str | None = None, *, clear_log: bool = True) -> dict:
    """
    轻量 token 探测查活：不重新登录、不烧 OTP，协议无关。

    用账号库现有 access_token 打套餐查询接口探测：
      - http_status==200            → status=live（有效）
      - http_status==401/token_expired → status=deactivated（失效）
      - 其他（403 风控/网络异常/无 token）→ status=failed（不确定，可降级完整重登录）

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        checked_at: ISO,
        http_status: int?,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")

    checked_at = _now()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    key = email.lower()
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    fh: logging.FileHandler | None = None
    try:
        fh = _make_file_handler(path)
        logging.getLogger().addHandler(fh)
        logger.info("[查活-轻量] 日志文件：%s", path)
        return _probe_liveness_light(email, proxy, checked_at)
    finally:
        try:
            logger.info("[查活-轻量] 结束：%s", email)
            if fh is not None:
                logging.getLogger().removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)


def _check_liveness_full(email: str, proxy: str | None, checked_at: str) -> dict:
    """完整重新登录查活（Providers → OTP → OAuth callback → Session/AT）。"""
    try:
        logger.info("[查活] 开始重新登录：%s", email)
        logger.info("[查活] 流程：Providers → CSRF → Signin → Authorize → 邮箱 OTP → OAuth callback → Session/AT")
        session, authorize_url = _network_preflight_with_retry(email, proxy)

        otp_after_ts = time.time()
        final_url = follow_authorize(session, authorize_url)
        dead_code = detect_account_unusable_text(final_url)
        if dead_code:
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": dead_code}

        validate_result = _validate_with_retry(session, email, otp_after_ts)
        page = validate_result.get("page") if isinstance(validate_result, dict) else {}
        page = page if isinstance(page, dict) else {}
        page_type = str(page.get("type") or "")
        continue_url = (
            validate_result.get("continue_url")
            or validate_result.get("external_url")
            or validate_result.get("url")
            or page.get("continue_url")
            or page.get("external_url")
            or page.get("url")
        )
        if not continue_url:
            raise RuntimeError(f"OTP 登录成功但没有 OAuth continue_url: {validate_result}")
        if "about-you" in str(continue_url) or page_type in {"about_you", "about-you"}:
            raise RuntimeError(f"该邮箱登录后进入资料页，疑似不是完整已注册账号: page_type={page_type}, continue_url={continue_url}")

        follow_oauth_callback(session, str(continue_url), referer="https://auth.openai.com/email-verification")
        session_info = fetch_session(session)
        access_token = str(session_info.get("accessToken") or "")
        if not access_token:
            raise RuntimeError("重新登录后未拿到 accessToken")

        user = session_info.get("user") or {}
        account = session_info.get("account") or {}
        logger.info("[查活] 正常：%s user_id=%s plan=%s", email, user.get("id"), account.get("planType"))
        return {
            "ok": True,
            "status": "live",
            "checked_at": checked_at,
            "access_token": access_token,
            "session": session_info,
            "device_id": session.device_id,
            "proxy_used": session.proxy or None,
        }
    except AccountUnusableError as exc:
        code = getattr(exc, "error_code", "") or detect_account_unusable_text(str(exc)) or "account_deactivated"
        logger.warning("[查活] 已废号：%s %s", email, code)
        return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
    except Exception as exc:
        code = detect_account_unusable_text(str(exc))
        if code:
            logger.warning("[查活] 已废号：%s %s", email, code)
            return {"ok": False, "status": "deactivated", "checked_at": checked_at, "error": code}
        logger.warning("[查活] 失败：%s %s: %s", email, type(exc).__name__, str(exc)[:260])
        return {"ok": False, "status": "failed", "checked_at": checked_at, "error": f"{type(exc).__name__}: {str(exc)[:500]}"}


def check_account_liveness(
    email: str,
    proxy: str | None = None,
    *,
    clear_log: bool = True,
    mode: str = "auto",
) -> dict:
    """
    查活入口。

    mode:
      - light: 只用现有 token 轻量探测，不重新登录、不烧 OTP
      - full:  完整重新登录（旧行为）
      - auto（默认）: 先 light 探测；live/deactivated 直接返回，
        failed（403/风控/网络异常/无 token，不确定）降级完整重登录确认

    返回：
      {
        ok: bool,
        status: live/deactivated/failed,
        access_token: str?,
        session: dict?,
        checked_at: ISO,
        error: str?
      }
    """
    email = str(email or "").strip()
    if not email:
        raise ValueError("email 不能为空")
    mode = str(mode or "auto").strip().lower()
    if mode not in _VALID_MODES:
        raise ValueError(f"mode={mode!r} 无效，可选 full / light / auto")

    checked_at = _now()
    path = log_path(email)
    path.parent.mkdir(parents=True, exist_ok=True)
    if clear_log:
        path.write_text("", encoding="utf-8")

    key = email.lower()
    with _RUNNING_LOCK:
        _RUNNING.add(key)
    fh: logging.FileHandler | None = None
    try:
        fh = _make_file_handler(path)
        logging.getLogger().addHandler(fh)
        logger.info("[查活] 日志文件：%s", path)

        if mode == "light":
            return _probe_liveness_light(email, proxy, checked_at)

        if mode == "auto":
            light = _probe_liveness_light(email, proxy, checked_at)
            if light.get("status") in {"live", "deactivated"}:
                return light
            logger.info("[查活] 轻量探测不确定（%s），降级完整重新登录确认：%s", light.get("error"), email)
        return _check_liveness_full(email, proxy, checked_at)
    finally:
        try:
            logger.info("[查活] 结束：%s", email)
            if fh is not None:
                logging.getLogger().removeHandler(fh)
                fh.close()
        finally:
            with _RUNNING_LOCK:
                _RUNNING.discard(key)
