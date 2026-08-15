# -*- coding: utf-8 -*-
"""
接码平台客户端。

用于 Codex OAuth "全新 session" 流程过 OpenAI 的 /phone-verification 手机号验证：
    1. acquire_number()       getNumber 取一个手机号（返回 激活ID + 号码）
    2. wait_for_sms_code()    轮询 getStatus 直到拿到短信验证码
    3. complete() / cancel()  setStatus 标记完成(6) / 取消(8)

当前支持：
    - GrizzlySMS：GET 文本接口，文档 https://api.grizzlysms.com
    - L：本地 JSON 管理接口，文档 L_API.md
    - H：本地 JSON 管理接口，文档 H_API.md

价格相关：每取一个号、收到短信都会计费，所以：
    - 取号后若收不到短信，必须 cancel(8) 释放，避免白扣钱；
    - 成功拿到码后 complete(6) 正式完成激活。
"""
import json
import logging
import re
import threading
import time
from pathlib import Path
from urllib.parse import urljoin

from curl_cffi.requests import Session as CurlSession

# 注意：用 `from config import codex` 而不是 `from config.codex import X`，
# 这样 WebUI 调 config.reload_all() 后，本模块通过 codex.X 读到的是最新值。
from config import codex as _cfg
from config import IMPERSONATE

logger = logging.getLogger(__name__)

# GrizzlySMS 规则：号码取出后 2 分钟内不允许取消（防薅号）。
# 这里留 5 秒缓冲，时间到了再发 setStatus=8。
_MIN_CANCEL_DELAY = 125

# 记录每个 activation_id 的取号时间，供 cancel() 判断是否要等。
# 用模块级 dict 而不是改 acquire_number 返回值，保持向后兼容。
_ACQUIRED_AT: dict[str, float] = {}


class SmsProviderError(RuntimeError):
    """接码平台通用错误。"""


class SmsNoNumbersError(SmsProviderError):
    """暂无可用号码（NO_NUMBERS），可换国家或稍后重试。"""


class SmsNoBalanceError(SmsProviderError):
    """余额不足（NO_BALANCE），必须充值，重试无意义——上层应立即停止。"""


class SmsCodeTimeout(SmsProviderError):
    """单个号等短信超时（OpenAI 没发或没到达）。"""


class SmsQueueExhaustedError(SmsProviderError):
    """整队列多轮重试后仍失败（平台无号 / 平台接口异常）。"""


def _http() -> CurlSession:
    s = CurlSession(impersonate=IMPERSONATE)
    s.timeout = _cfg.SMS_REQUEST_TIMEOUT
    return s


def _provider() -> str:
    return str(getattr(_cfg, "SMS_PROVIDER", "grizzly") or "grizzly").strip().lower()


def _request_grizzly(http: CurlSession, params: dict) -> str:
    """
    发一个 GrizzlySMS API 请求，返回去空白的响应文本。
    统一识别公共错误码并抛对应异常。
    """
    base_params = {"api_key": _cfg.SMS_API_KEY}
    base_params.update(params)
    resp = http.get(_cfg.SMS_API_BASE, params=base_params)
    if resp.status_code != 200:
        raise SmsProviderError(
            f"GrizzlySMS HTTP {resp.status_code}: {(resp.text or '')[:200]}"
        )
    text = (resp.text or "").strip()

    # 公共错误码（任何 action 都可能返回）
    if text == "BAD_KEY":
        raise SmsProviderError("接码平台 API key 无效（BAD_KEY）")
    if text == "NO_BALANCE":
        raise SmsNoBalanceError("接码平台余额不足（NO_BALANCE），请充值")
    if text == "NO_NUMBERS":
        raise SmsNoNumbersError("接码平台暂无可用号码（NO_NUMBERS）")
    if text == "SERVICE_UNAVAILABLE_REGION":
        raise SmsProviderError("接码平台地区受限（SERVICE_UNAVAILABLE_REGION），请换 IP")
    if text in ("BAD_ACTION", "BAD_SERVICE", "BAD_STATUS"):
        raise SmsProviderError(f"接码平台请求参数错误：{text}")
    if text == "NO_ACTIVATION":
        raise SmsProviderError("激活 ID 不存在（NO_ACTIVATION）")
    if text.startswith("The service is prohibited"):
        raise SmsProviderError(f"该服务被平台禁售：{text}")

    return text


def _l_url(path: str) -> str:
    base = str(getattr(_cfg, "L_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("L_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _l_headers() -> dict:
    token = str(getattr(_cfg, "L_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("L_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_l_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_l_url(path), headers=_l_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"L HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"L 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"L 暂无可用号码：{combined}")
        raise SmsProviderError(f"L 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"L 响应不是 JSON 对象：{text[:200]}")
    return data


def _h_url(path: str) -> str:
    base = str(getattr(_cfg, "H_API_BASE", "") or "").strip()
    if not base:
        raise SmsProviderError("H_API_BASE 不能为空")
    return urljoin(base.rstrip("/") + "/", path.lstrip("/"))


def _h_headers() -> dict:
    token = str(getattr(_cfg, "H_ADMIN_AUTH_CODE", "") or "").strip()
    if not token:
        raise SmsProviderError("H_ADMIN_AUTH_CODE 不能为空")
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def _post_h_json(http: CurlSession, path: str, payload: dict) -> dict:
    resp = http.post(_h_url(path), headers=_h_headers(), data=json.dumps(payload))
    text = (resp.text or "").strip()
    try:
        data = resp.json()
    except Exception:
        data = {}

    if resp.status_code != 200:
        msg = data.get("error") if isinstance(data, dict) else ""
        raise SmsProviderError(f"H HTTP {resp.status_code}: {(msg or text)[:200]}")
    if isinstance(data, dict) and data.get("error"):
        error = str(data.get("error") or "")
        raw = str(data.get("raw") or "")
        combined = f"{error} {raw}".strip()
        if "NO_BALANCE" in combined or "余额不足" in combined:
            raise SmsNoBalanceError(f"H 余额不足：{combined}")
        if "NO_NUMBERS" in combined or "暂无号码" in combined:
            raise SmsNoNumbersError(f"H 暂无可用号码：{combined}")
        raise SmsProviderError(f"H 请求失败：{combined}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"H 响应不是 JSON 对象：{text[:200]}")
    return data


def _release_h_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 H_API /api/admin/h/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("H release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"H release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:H] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_h_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 H 号码。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("H release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_h_json(http, "/api/admin/h/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:H] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _release_l_number(activation_id: str, http: CurlSession | None = None) -> dict:
    """调用 L_API /api/admin/l/release 释放单个号码。"""
    activation_id = str(activation_id or "").strip()
    if not activation_id:
        raise SmsProviderError("L release 缺少 id")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"id": activation_id})
        failed = data.get("failed") if isinstance(data, dict) else None
        if isinstance(failed, list) and failed:
            # 接口允许部分失败。单个释放时 failed 非空基本代表这个 id 释放失败。
            detail = json.dumps(failed, ensure_ascii=False)[:300]
            raise SmsProviderError(f"L release 失败 id={activation_id}: {detail}")
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        logger.info(f"[SMS:L] 已释放号码 id={activation_id}, released={released}")
        _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def release_l_numbers(ids: list[str], http: CurlSession | None = None) -> dict:
    """批量释放 L 号码，供工具/后续批处理复用。"""
    ids = [str(x or "").strip() for x in (ids or []) if str(x or "").strip()]
    if not ids:
        raise SmsProviderError("L release 缺少 ids")
    own_http = http is None
    http = http or _http()
    try:
        data = _post_l_json(http, "/api/admin/l/release", {"ids": ids})
        released = data.get("released", data.get("updated", 0)) if isinstance(data, dict) else 0
        failed = data.get("failed") if isinstance(data, dict) else []
        logger.info(f"[SMS:L] 批量释放号码完成 released={released}, failed={len(failed) if isinstance(failed, list) else 0}")
        for activation_id in ids:
            _ACQUIRED_AT.pop(activation_id, None)
        return data
    finally:
        if own_http:
            http.close()


def _normalize_phone_digits(value: str) -> str:
    """把平台返回/配置的号码片段规范化为纯数字，避免 +-849... 这类非法 E.164。"""
    return "".join(ch for ch in str(value or "").strip() if ch.isdigit())


def _normalize_l_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "L_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _normalize_h_phone(phone: str) -> str:
    phone = _normalize_phone_digits(phone)
    prefix = _normalize_phone_digits(getattr(_cfg, "H_PHONE_PREFIX", ""))
    if prefix and phone and not phone.startswith(prefix):
        return f"{prefix}{phone}"
    return phone


def _validate_phone_country(phone: str, country: str) -> str | None:
    """校验取到的号码区号是否匹配请求国家；匹配返回 None，不匹配返回原因。

    国家码未知时返回 None（跳过校验不误伤，打 DEBUG 日志）。
    phone 为不带 + 的号码（可为带格式的原始值，内部规范化）。
    """
    country = str(country or "").strip()
    dial_code = _COUNTRY_DIAL_CODES.get(country)
    if not dial_code:
        logger.debug(f"[SMS] 国家码 {country or '-'} 无区号映射，跳过号码区号校验")
        return None
    digits = _normalize_phone_digits(phone)
    if not digits:
        return None
    if digits.startswith(dial_code):
        return None
    return f"号码 +{digits} 区号不匹配请求国家 {country}（应 +{dial_code}）"


def _reject_wrong_country_phone(
    activation_id: str, phone: str, country: str, http: CurlSession | None
) -> None:
    """区号校验不匹配：记录 WARNING、取消该号并抛 SmsNoNumbersError（切下一国家）。

    平台串号视为该国号码不可用（不重复 try 同一号），由共享循环切下一国家。
    """
    reason = _validate_phone_country(phone, country)
    if not reason:
        return
    logger.warning(f"[SMS] 平台串号：{reason}，id={activation_id}，已取消换号")
    try:
        cancel(activation_id, http=http)
    except Exception as exc:
        logger.warning(f"[SMS] 串号取消失败（继续换号）：{exc}")
    raise SmsNoNumbersError(reason)


def _h_phone_acquire_mode() -> str:
    """
    H 取号模式：
      - reusable/reuse/prefer_reuse：优先复用，调用 /api/admin/h/take-reusable-phone
      - new/fresh/always_new：每次取新号，调用 /api/admin/h/take-phone
    """
    raw = str(getattr(_cfg, "H_PHONE_ACQUIRE_MODE", "reusable") or "reusable").strip().lower()
    if raw in ("new", "fresh", "always_new", "take_phone", "take-phone", "每次取新号", "新号"):
        return "new"
    return "reusable"


# ============================================================
# 取号
# ============================================================

def acquire_number(
    http: CurlSession | None = None,
    service: str | None = None,
    country: str | None = None,
) -> tuple[str, str]:
    """
    取一个手机号（getNumber）。

    Returns:
        (activation_id, phone_number) —— phone_number 不带 + 前缀（如 16195366483）

    Raises:
        SmsNoNumbersError / SmsNoBalanceError / SmsProviderError
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            payload = {
                "service": service or _cfg.SMS_SERVICE,
                "country": country or _cfg.SMS_COUNTRY,
            }
            if _cfg.SMS_MAX_PRICE:
                payload["maxPrice"] = _cfg.SMS_MAX_PRICE

            data = _post_l_json(http, "/api/admin/l/take-phone", payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "L_PHONE_PREFIX", "") or "")
            phone = _normalize_l_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:L] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"L take-phone 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            _reject_wrong_country_phone(activation_id, phone, country or _cfg.SMS_COUNTRY, http)
            logger.info(f"[SMS:L] 取号成功：id={activation_id}, phone=+{phone}")
            return activation_id, phone

        if _provider() == "h":
            # H_API 使用 projectId + country；统一复用 SMS_SERVICE / SMS_COUNTRY，
            # 避免接码平台之间出现重复的“服务/国家”配置。
            project_id = str(service or _cfg.SMS_SERVICE).strip()
            h_country = str(country or _cfg.SMS_COUNTRY).strip()
            if not project_id:
                raise SmsProviderError("H projectId 不能为空：请填写 SMS_SERVICE")
            if not h_country:
                raise SmsProviderError("H country 不能为空：请填写 SMS_COUNTRY")
            payload = {
                "projectId": project_id,
                "country": h_country,
            }
            mode = _h_phone_acquire_mode()
            api_path = "/api/admin/h/take-phone" if mode == "new" else "/api/admin/h/take-reusable-phone"
            data = _post_h_json(http, api_path, payload)
            item = data.get("item") or {}
            activation_id = str(item.get("id") or "").strip()
            raw_phone = str(item.get("phone") or "")
            raw_prefix = str(getattr(_cfg, "H_PHONE_PREFIX", "") or "")
            phone = _normalize_h_phone(raw_phone)
            if raw_phone.strip() != phone or raw_prefix.strip():
                logger.info(
                    f"[SMS:H] 号码规范化：raw_phone={raw_phone!r}, "
                    f"prefix={raw_prefix!r}, normalized=+{phone}"
                )
            if not activation_id or not phone:
                raise SmsProviderError(f"H {api_path.rsplit('/', 1)[-1]} 响应缺少 item.id/item.phone：{str(data)[:200]}")
            _ACQUIRED_AT[activation_id] = time.time()
            _reject_wrong_country_phone(activation_id, phone, h_country, http)
            logger.info(
                f"[SMS:H] 取号成功：mode={mode}, api={api_path}, id={activation_id}, phone=+{phone}, "
                f"reused={bool(data.get('reused'))}, duplicate={bool(data.get('duplicate'))}"
            )
            return activation_id, phone

        params = {
            "action": "getNumber",
            "service": service or _cfg.SMS_SERVICE,
            "country": country or _cfg.SMS_COUNTRY,
        }
        if _cfg.SMS_MAX_PRICE:
            params["maxPrice"] = _cfg.SMS_MAX_PRICE

        text = _request_grizzly(http, params)
        # 成功格式：ACCESS_NUMBER:激活ID:号码
        if not text.startswith("ACCESS_NUMBER:"):
            raise SmsProviderError(f"getNumber 非预期响应：{text[:200]}")
        parts = text.split(":")
        if len(parts) < 3:
            raise SmsProviderError(f"getNumber 响应格式异常：{text[:200]}")
        activation_id = parts[1].strip()
        phone = parts[2].strip()
        _ACQUIRED_AT[activation_id] = time.time()
        _reject_wrong_country_phone(activation_id, phone, country or _cfg.SMS_COUNTRY, http)
        logger.info(f"[SMS] 取号成功：activation_id={activation_id}, phone=+{phone}")
        return activation_id, phone
    finally:
        if own_http:
            http.close()


# ============================================================
# 取短信验证码
# ============================================================

def wait_for_sms_code(
    activation_id: str,
    http: CurlSession | None = None,
    max_wait: int | None = None,
    poll_interval: int | None = None,
) -> str:
    """
    轮询 getStatus 直到拿到短信验证码。

    Returns:
        验证码字符串

    Raises:
        SmsCodeTimeout —— 超时没收到（上层可换号重试）
        SmsProviderError —— 激活被取消等
    """
    own_http = http is None
    http = http or _http()
    deadline = time.time() + (max_wait or _cfg.SMS_CODE_WAIT)
    interval = poll_interval or _cfg.SMS_POLL_INTERVAL
    try:
        provider = _provider()
        total_wait = max_wait or _cfg.SMS_CODE_WAIT
        logger.info(f"[SMS] 等待短信验证码 activation_id={activation_id}，最长 {total_wait}s...")
        round_no = 0
        while time.time() < deadline:
            try:
                from core.registration_service import check_stop_requested
                check_stop_requested()
            except ImportError:
                pass
            round_no += 1
            elapsed = max(0, int(total_wait - max(0, deadline - time.time())))
            remaining_before = max(0, int(deadline - time.time()))
            logger.info(
                f"[SMS] 第 {round_no} 轮获取验证码 activation_id={activation_id}，"
                f"已等 {elapsed}s，剩余约 {remaining_before}s"
            )
            if provider == "l":
                data = _post_l_json(http, "/api/admin/l/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:L] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:L] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            if provider == "h":
                data = _post_h_json(http, "/api/admin/h/fetch-code", {"id": activation_id})
                code = str(data.get("code") or "").strip()
                raw = str(data.get("raw") or "").strip()
                status = str((data.get("item") or {}).get("status") or "").strip()
                if code:
                    logger.info(f"[SMS:H] 第 {round_no} 轮收到验证码：{code}")
                    return code
                remaining = max(0, int(deadline - time.time()))
                logger.info(
                    f"[SMS:H] 第 {round_no} 轮未收到验证码，状态={status or raw or 'WAIT'}，"
                    f"{interval}s 后重试（剩余 {remaining}s）"
                )
                time.sleep(interval)
                continue

            text = _request_grizzly(http, {"action": "getStatus", "id": activation_id})

            if text.startswith("STATUS_OK:"):
                code = text.split(":", 1)[1].strip()
                logger.info(f"[SMS] 第 {round_no} 轮收到验证码：{code}")
                return code
            if text == "STATUS_CANCEL":
                raise SmsProviderError("激活已被取消（STATUS_CANCEL）")
            # STATUS_WAIT_CODE / STATUS_WAIT_RETRY:* / STATUS_WAIT_RESEND → 继续等
            remaining = max(0, int(deadline - time.time()))
            logger.info(f"[SMS] 第 {round_no} 轮未收到验证码，状态={text}，{interval}s 后重试（剩余 {remaining}s）")
            time.sleep(interval)

        raise SmsCodeTimeout(f"等待短信超时（>{total_wait}s），activation_id={activation_id}")
    finally:
        if own_http:
            http.close()


# ============================================================
# 改状态
# ============================================================

def set_status(activation_id: str, status: int, http: CurlSession | None = None) -> str:
    """
    设置激活状态（setStatus）。
        1 = 号码已就绪（短信已发出）
        3 = 等下一条短信（重发）
        6 = 完成激活
        8 = 取消激活
    """
    own_http = http is None
    http = http or _http()
    try:
        if _provider() == "l":
            logger.debug(f"[SMS:L] 忽略状态设置 id={activation_id}, status={status}")
            return "OK"
        return _request_grizzly(http, {"action": "setStatus", "status": str(status), "id": activation_id})
    finally:
        if own_http:
            http.close()


def complete(activation_id: str, http: CurlSession | None = None) -> None:
    """标记激活完成（status=6）。失败只告警不抛，避免影响主流程。"""
    if _provider() == "l":
        logger.info(f"[SMS:L] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    if _provider() == "h":
        # H 成功 fetch-code 后后台会自动按多次收码策略重取；这里不 release。
        logger.info(f"[SMS:H] 已完成 id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
        return
    try:
        set_status(activation_id, 6, http=http)
        logger.info(f"[SMS] 已标记完成 activation_id={activation_id}")
        _ACQUIRED_AT.pop(activation_id, None)
    except Exception as exc:
        logger.warning(f"[SMS] 标记完成失败（不影响结果）：{exc}")


def _do_cancel_sync(activation_id: str, http_factory) -> None:
    """实际的同步取消逻辑：等够 2 分钟限制 → 发请求 → 失败重试一次。"""
    acquired_at = _ACQUIRED_AT.get(activation_id)
    if acquired_at is not None:
        elapsed = time.time() - acquired_at
        if elapsed < _MIN_CANCEL_DELAY:
            wait = _MIN_CANCEL_DELAY - elapsed
            logger.info(
                f"[SMS] 取消等待 GrizzlySMS 2 分钟限制：activation_id={activation_id}，"
                f"还需等 {wait:.0f}s..."
            )
            time.sleep(wait)

    # 后台线程不能复用外部 http session（curl_cffi 非线程安全），自己建一个
    http = http_factory()
    try:
        for attempt in range(1, 3):
            try:
                set_status(activation_id, 8, http=http)
                logger.info(f"[SMS] 已取消 activation_id={activation_id}")
                _ACQUIRED_AT.pop(activation_id, None)
                return
            except Exception as exc:
                if attempt == 1:
                    logger.warning(f"[SMS] 取消失败（{exc}），5s 后重试...")
                    time.sleep(5)
                else:
                    logger.warning(
                        f"[SMS] 取消最终失败（不影响结果，需到平台手动取消）：activation_id={activation_id}, {exc}"
                    )
    finally:
        try:
            http.close()
        except Exception:
            pass


def cancel(activation_id: str, http: CurlSession | None = None, background: bool = True) -> None:
    """
    取消激活（status=8），释放号码避免白扣费。

    GrizzlySMS 规则：号码取出后约 2 分钟内不允许取消。本函数默认 background=True，
    把"等 2 分钟+取消"放到后台守护线程里执行，主流程立刻返回继续走（如换下一个号），
    避免被这 2 分钟阻塞。

    background=False 时同步等够时间再返回（少数场景需要确认取消完成时用）。

    失败只告警不抛，不影响主流程。
    """
    if _provider() == "l":
        try:
            _release_l_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:L] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return
    if _provider() == "h":
        try:
            _release_h_number(activation_id, http=http)
        except Exception as exc:
            logger.warning(f"[SMS:H] 释放号码失败（不影响主流程）：id={activation_id}, {type(exc).__name__}: {exc}")
            _ACQUIRED_AT.pop(activation_id, None)
        return

    if not background:
        _do_cancel_sync(activation_id, _http)
        return

    t = threading.Thread(
        target=_do_cancel_sync,
        args=(activation_id, _http),
        name=f"sms-cancel-{activation_id}",
        daemon=True,
    )
    t.start()
    logger.debug(f"[SMS] 取消任务已派后台：activation_id={activation_id}")


# ============================================================
# 国家优先级队列（统一入口）
# ============================================================

# 兜底默认队列（原 codex_oauth / roxy_codex_oauth 硬编码值）
_DEFAULT_COUNTRY_QUEUE = ["54", "76", "73", "33"]

# 本地接码成功率埋点文件：{country: {success, failed}}
_SMS_STATS_FILE = Path(__file__).resolve().parent.parent / "data" / "sms_country_stats.json"
_SMS_STATS_LOCK = threading.Lock()

# iCloud 工作流等调用方选中的国家，前置到队列头（优先级最高）；批次结束后清空
_SMS_COUNTRY_PREFER: str = ""

# 注册任务按次指定的接码国家/排序覆盖（threading.local，只对当前任务线程生效，
# 任务结束 clear_task_sms_override 清除，避免污染线程池复用的其他任务）
_SMS_THREAD_CTX = threading.local()

# auto 排序时过滤成功率低于该阈值的国家
_MIN_SUCCESS_RATE = 0.3

# 判定「平台接口异常」的连续失败阈值：连续达到该次数即不再归因于无号
_PLATFORM_ERROR_THRESHOLD = 5

# 国家码 → 国际区号（取号后校验号码区号，防平台串号）。
# 平台为 sms-activate 兼容编号（33=哥伦比亚 / 73=巴西 / 187=美国），
# 另含备用码（57=哥伦比亚区号 / 6=巴西备用）；未知国家码跳过校验不误伤。
_COUNTRY_DIAL_CODES: dict[str, str] = {
    "33": "57",    # 哥伦比亚
    "57": "57",    # 哥伦比亚（备用码）
    "73": "55",    # 巴西
    "6": "55",     # 巴西（备用码）
    "187": "1",    # 美国
    "1": "1",      # 美国（备用码）
    "54": "52",    # 墨西哥
    "76": "244",   # 安哥拉
}

# 平台热门国家作为兜底候选补充时的上限（逐国家查询价格/库存，避免请求数过多）
_TOP_COUNTRIES_CANDIDATE_LIMIT = 10


def set_country_prefer(country: str | None) -> None:
    """设置/清空"前置到队列头"的国家（如 iCloud 工作流选的接码国家）。"""
    global _SMS_COUNTRY_PREFER
    _SMS_COUNTRY_PREFER = str(country or "").strip()


def set_task_sms_override(country: str | None, sort: str | None) -> None:
    """设置当前线程（注册任务）的接码国家/排序覆盖；只对当前线程生效，不改全局配置。"""
    _SMS_THREAD_CTX.country = str(country or "").strip()
    _SMS_THREAD_CTX.sort = str(sort or "").strip().lower()


def clear_task_sms_override() -> None:
    """清除当前线程的接码覆盖（任务结束/线程复用前必须调用）。"""
    for attr in ("country", "sort"):
        try:
            delattr(_SMS_THREAD_CTX, attr)
        except AttributeError:
            pass


def _task_country_prefer() -> str:
    return str(getattr(_SMS_THREAD_CTX, "country", "") or "").strip()


def _task_sort() -> str:
    return str(getattr(_SMS_THREAD_CTX, "sort", "") or "").strip().lower()


def _prepend_prefer(queue: list[str], prefer: str | None) -> list[str]:
    pref = str(
        prefer
        if prefer is not None
        else (_task_country_prefer() or _SMS_COUNTRY_PREFER or "")
    ).strip()
    if not pref:
        return list(queue)
    result = [pref]
    for country in queue:
        if country != pref:
            result.append(country)
    return result


def _manual_country_queue() -> list[str]:
    """manual 队列：SMS_COUNTRY（主）+ SMS_FALLBACK_COUNTRIES（备选，逗号分隔）去重。"""
    primary = str(getattr(_cfg, "SMS_COUNTRY", "") or "").strip()
    fallback = str(getattr(_cfg, "SMS_FALLBACK_COUNTRIES", "") or "").strip()
    pieces = [primary]
    if fallback:
        pieces.extend(re.split(r"[,，\s]+", fallback))
    queue = []
    for piece in pieces:
        piece = piece.strip()
        if piece and piece not in queue:
            queue.append(piece)
    return queue or list(_DEFAULT_COUNTRY_QUEUE)


def _grizzly_json_action(
    action: str, http: CurlSession | None = None, country: str | None = None
) -> dict:
    """调 Grizzly/HeroSMS 的 JSON 类 action（getPrices/getNumbersStatus/getTopCountriesByService）。

    getPrices / getNumbersStatus 要求 country 为数字（否则 HTTP 422
    "Param 'country' must be a number"）；getTopCountriesByService 不带 country。
    """
    params = {"action": action}
    country = str(country or "").strip()
    if country:
        params["country"] = country
    if _cfg.SMS_SERVICE:
        params["service"] = _cfg.SMS_SERVICE
    own_http = http is None
    http = http or _http()
    try:
        text = _request_grizzly(http, params)
    finally:
        if own_http:
            http.close()
    try:
        data = json.loads(text)
    except Exception:
        raise SmsProviderError(f"{action} 响应不是 JSON：{text[:200]}")
    if not isinstance(data, dict):
        raise SmsProviderError(f"{action} 响应不是 JSON 对象：{text[:200]}")
    return data


def _candidate_countries() -> list[str]:
    """候选国家子集：manual 主队列 + prefer + 平台热门（补充，上限 _TOP_COUNTRIES_CANDIDATE_LIMIT）。

    GrizzlySMS/HeroSMS 的 getPrices/getNumbersStatus 只支持按 country 查询，
    无法一次拉全平台库存；auto 排序/兜底池只能在候选子集内做价格排序。
    """
    candidates = []
    for country in _manual_country_queue():
        if country not in candidates:
            candidates.append(country)
    pref = _task_country_prefer() or _SMS_COUNTRY_PREFER
    if pref and pref not in candidates:
        candidates.append(pref)
    try:
        top = _top_countries_from_api()[:_TOP_COUNTRIES_CANDIDATE_LIMIT]
    except Exception as exc:
        logger.warning(f"[SMS] 拉取热门国家补充候选失败（忽略）：{exc}")
        top = []
    for country in top:
        if country not in candidates:
            candidates.append(country)
    return candidates


def _fetch_price_info(countries: list[str] | None = None) -> tuple[dict, dict]:
    """返回 (prices, numbers_status)：{country: 价格} 与 {country: 可用号量}。

    GrizzlySMS/HeroSMS 的 getPrices/getNumbersStatus 要求 country 参数为数字，
    这里按候选国家子集逐国家查询（候选 = 主队列 + prefer + 平台热门补充），
    避免 422 全平台拉取；单国家查询失败只跳过该国并告警。
    """
    countries = list(
        dict.fromkeys(str(c).strip() for c in (countries or _candidate_countries()) if str(c or "").strip())
    )
    service = _cfg.SMS_SERVICE
    prices: dict[str, float] = {}
    status: dict[str, int] = {}
    last_exc = None
    # 共享一个会话逐国家查询，避免每个国家/每次 action 都新建 curl 会话（N+1 开销）
    own_http = True
    http = _http()
    try:
        for country in countries:
            try:
                prices_raw = _grizzly_json_action("getPrices", http=http, country=country)
                status_raw = _grizzly_json_action("getNumbersStatus", http=http, country=country)
            except SmsNoBalanceError:
                raise
            except Exception as exc:
                last_exc = exc
                logger.warning(f"[SMS] 国家 {country} 价格/库存查询失败（跳过该国）：{exc}")
                continue
            for c, val in (prices_raw or {}).items():
                service_val = val.get(service) if isinstance(val, dict) else None
                if isinstance(service_val, dict):
                    try:
                        prices[str(c)] = float(service_val.get("cost") or 0)
                    except (TypeError, ValueError):
                        pass
            for c, val in (status_raw or {}).items():
                if isinstance(val, dict):
                    count = val.get(service)
                    if isinstance(count, (int, float)) and count > 0:
                        status[str(c)] = int(count)
                elif isinstance(val, (int, float)) and val > 0:
                    status[str(c)] = int(val)
        if not prices and not status:
            raise SmsProviderError(
                f"候选国家价格/库存查询全部失败（{len(countries)} 个国家）"
                + (f"：{last_exc}" if last_exc else "")
            )
        return prices, status
    finally:
        if own_http:
            http.close()


def _load_sms_stats() -> dict:
    try:
        if _SMS_STATS_FILE.exists():
            data = json.loads(_SMS_STATS_FILE.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        logger.warning(f"[SMS] 读取本地成功率埋点失败（忽略）：{exc}")
    return {}


def _save_sms_stats(stats: dict) -> None:
    try:
        _SMS_STATS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _SMS_STATS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(_SMS_STATS_FILE)
    except Exception as exc:
        logger.warning(f"[SMS] 保存本地成功率埋点失败（不影响主流程）：{exc}")


def record_sms_result(country: str, ok: bool) -> None:
    """记录一次接码结果（成功/失败），供 auto 排序成功率使用。"""
    country = str(country or "").strip()
    if not country:
        return
    with _SMS_STATS_LOCK:
        stats = _load_sms_stats()
        entry = stats.setdefault(country, {"success": 0, "failed": 0})
        key = "success" if ok else "failed"
        entry[key] = int(entry.get(key, 0) or 0) + 1
        _save_sms_stats(stats)


def local_country_success_rates() -> dict[str, float]:
    """本地埋点成功率：{country: success/(success+failed)}。"""
    stats = _load_sms_stats()
    rates: dict[str, float] = {}
    for country, entry in stats.items():
        if not isinstance(entry, dict):
            continue
        success = int(entry.get("success", 0) or 0)
        failed = int(entry.get("failed", 0) or 0)
        total = success + failed
        if total > 0:
            rates[str(country)] = success / total
    return rates


def _top_countries_from_api() -> list[str]:
    """getTopCountriesByService 解析，兼容两种常见返回格式：

    - 扁平：{"54": 100, "73": 50}（country → count）
    - 嵌套（sms-online/GrizzlySMS 风格）：{"0": {"country": 54, "count": 100}}，
      外层是数字序号，国家码在 "country" 字段
    """
    data = _grizzly_json_action("getTopCountriesByService")
    ordered = []
    for key, val in (data or {}).items():
        if isinstance(val, (int, float)) and val > 0:
            ordered.append((str(key), val))
        elif isinstance(val, dict):
            country = val.get("country")
            count = val.get("count")
            if isinstance(count, (int, float)) and count > 0 and country is not None:
                ordered.append((str(country), count))
    ordered.sort(key=lambda item: item[1], reverse=True)
    return [country for country, _ in ordered]


def _success_rates_for_auto() -> dict[str, float]:
    """auto 排序用成功率：本地埋点优先，无历史国家用平台热门兜底（冷启动给中性值）。"""
    rates = local_country_success_rates()
    try:
        top = _top_countries_from_api()
    except Exception as exc:
        logger.warning(f"[SMS] 拉取热门国家失败（冷启动兜底不可用）：{exc}")
        top = []
    for country in top:
        rates.setdefault(country, 0.5)
    return rates


def _auto_sorted_country_queue(sort: str) -> list[str]:
    if _provider() != "grizzly":
        return _manual_country_queue()
    prices, status = _fetch_price_info()
    rates = _success_rates_for_auto()
    candidates = []
    for country, available in status.items():
        rate = rates.get(country)
        if rate is None or rate < _MIN_SUCCESS_RATE:
            continue
        candidates.append({
            "country": country,
            "price": prices.get(country, float("inf")),
            "success": rate,
        })
    if sort == "auto_success":
        candidates.sort(key=lambda c: (-c["success"], c["price"]))
    else:
        candidates.sort(key=lambda c: (c["price"], -c["success"]))
    return [c["country"] for c in candidates] or _manual_country_queue()


def resolve_country_queue(prefer: str | None = None, sort: str | None = None) -> list[str]:
    """生成有序去重的国家优先级队列。

    sort:
        "manual"       = SMS_COUNTRY(主) + SMS_FALLBACK_COUNTRIES(备选) 去重
        "auto_price"   = 价格升序(主) + 成功率降序(次)，过滤 0 库存/低成功率国家
        "auto_success" = 成功率优先(主) + 价格兜底(次)
    为空或省略时读配置 SMS_COUNTRY_SORT。API 拉取失败自动回落 manual。
    prefer 非空时前置到队列头（去重），优先级最高。
    线程级 set_task_sms_override 覆盖优先于配置（按次任务参数）。
    """
    sort = (
        str(sort or "").strip().lower()
        or _task_sort()
        or str(getattr(_cfg, "SMS_COUNTRY_SORT", "") or "").strip().lower()
    )
    if sort in ("auto_price", "auto_success"):
        try:
            queue = _auto_sorted_country_queue(sort)
        except SmsNoBalanceError:
            raise
        except Exception as exc:
            logger.warning(f"[SMS] 国家排序（{sort}）拉取失败，回落 manual 队列：{exc}")
            queue = _manual_country_queue()
    else:
        queue = _manual_country_queue()
    return _prepend_prefer(queue, prefer)


def _fallback_country_pool(base_queue: list[str], sort: str | None = None) -> list[str]:
    """
    grizzly 且 manual 排序时，追加「全平台有库存国家按价格升序」兜底池（排除主队列已试国家）。

    auto_price / auto_success 的主队列本身已是全平台排序结果，不再扩展；
    l/h 平台无价格数据，兜底回落主队列（靠多轮重试）。
    """
    sort = (
        str(sort or "").strip().lower()
        or _task_sort()
        or str(getattr(_cfg, "SMS_COUNTRY_SORT", "") or "").strip().lower()
    )
    if sort in ("auto_price", "auto_success"):
        return []
    if _provider() != "grizzly":
        return []
    try:
        pool = _auto_sorted_country_queue("auto_price")
    except SmsNoBalanceError:
        raise
    except Exception as exc:
        logger.warning(f"[SMS] 兜底国家池不可用，仅尝试主队列 {len(base_queue)} 个国家：{exc}")
        return []
    return [country for country in pool if country not in base_queue]


def _round_country_queue(sort: str | None = None, prefer: str | None = None) -> list[str]:
    """某一轮的完整国家队列：主队列 → 兜底池（去重）。"""
    base = resolve_country_queue(sort=sort, prefer=prefer)
    pool = _fallback_country_pool(base, sort=sort)
    if not pool:
        return base
    return base + pool


def run_country_queue_rounds(
    try_country,
    *,
    sort: str | None = None,
    prefer: str | None = None,
    max_retries: int | None = None,
    round_retries: int | None = None,
    round_wait: int | None = None,
    log_prefix: str = "[SMS]",
):
    """
    多轮国家队列取号循环（codex_oauth / roxy_codex_oauth 共享）。

    每轮按「主队列 → 兜底池」顺序尝试取号；整轮全失败等待 round_wait 秒进入下一轮；
    全部轮次仍失败 → 抛 SmsQueueExhaustedError 并按失败类型分层：
      - 无号主导 → 消息含「接码平台当前无可用号码，已重试 N 轮」
      - 平台接口异常达阈值（连续 5 次或占主导）→ 消息含「接码平台接口异常」
      - SmsNoBalanceError 立即透传（重试无意义）

    try_country(country, round_no, attempt) 执行单个国家的一次号码尝试：
      - 返回（任意值）即成功，循环立即返回该值
      - 抛 SmsNoNumbersError → 该国无可用号码，切下一国家（不耗 attempt/轮次）
      - 抛 SmsNoBalanceError → 立即透传
      - 抛 SmsProviderError（其他）→ 记为平台接口异常，同国家换号重试
    """
    max_retries = max(
        1, int(max_retries if max_retries is not None else (getattr(_cfg, "SMS_MAX_RETRIES", 10) or 10))
    )
    round_retries = max(
        1, int(round_retries if round_retries is not None else (getattr(_cfg, "SMS_ROUND_RETRIES", 3) or 3))
    )
    round_wait = max(
        0, int(round_wait if round_wait is not None else (getattr(_cfg, "SMS_ROUND_WAIT", 30) or 30))
    )

    no_numbers_total = 0
    platform_errors_total = 0
    consecutive_platform_errors = 0
    last_err = None

    for round_no in range(1, round_retries + 1):
        queue = _round_country_queue(sort=sort, prefer=prefer)
        if not queue:
            queue = _manual_country_queue()
        logger.info(f"{log_prefix} 第 {round_no}/{round_retries} 轮国家队列：{queue}")
        country_idx = 0
        attempts = 0
        round_exhausted = False
        while attempts < max_retries:
            country = queue[min(country_idx, len(queue) - 1)]
            try:
                result = try_country(country, round_no, attempts + 1)
                logger.info(f"{log_prefix} 第 {round_no} 轮取号成功 country={country}")
                return result
            except SmsNoBalanceError:
                raise
            except SmsNoNumbersError as exc:
                no_numbers_total += 1
                consecutive_platform_errors = 0
                last_err = exc
                if country_idx + 1 < len(queue):
                    country_idx += 1
                    logger.warning(
                        f"{log_prefix} 所选国家 {country} 无号，已 fallback 到 {queue[country_idx]}：{exc}"
                    )
                    continue
                logger.warning(f"{log_prefix} 第 {round_no} 轮所有国家均无号：{queue}")
                round_exhausted = True
                break
            except SmsProviderError as exc:
                attempts += 1
                platform_errors_total += 1
                consecutive_platform_errors += 1
                last_err = exc
                logger.warning(f"{log_prefix} 接码尝试失败（平台接口异常）：{exc}")
        if not round_exhausted:
            logger.warning(f"{log_prefix} 第 {round_no} 轮换号次数用完（{max_retries}）仍失败")
        if round_no < round_retries:
            logger.info(f"{log_prefix} 第 {round_no} 轮全失败，{round_wait}s 后进入下一轮...")
            time.sleep(round_wait)

    if consecutive_platform_errors >= _PLATFORM_ERROR_THRESHOLD or (
        platform_errors_total > 0 and platform_errors_total > no_numbers_total
    ):
        raise SmsQueueExhaustedError(
            f"{log_prefix} 接码平台接口异常，已重试 {round_retries} 轮仍失败"
            f"（平台错误 {platform_errors_total} 次，无号 {no_numbers_total} 次）"
            + (f"，最后错误：{last_err}" if last_err else "")
        )
    raise SmsQueueExhaustedError(
        f"{log_prefix} 接码平台当前无可用号码，已重试 {round_retries} 轮仍失败"
        + (f"，最后错误：{last_err}" if last_err else "")
    )
