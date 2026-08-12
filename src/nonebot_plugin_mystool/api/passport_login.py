import asyncio
import base64
import hashlib
import json
import secrets
import string
import time
from typing import Any, Dict, Optional, Tuple

import httpx
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import padding

from ..model import BBSCookies, BaseApiStatus
from ..utils import generate_device_id, logger

__all__ = [
    "build_aigis_response",
    "create_qr_login",
    "get_cookie_token_by_passport_stoken",
    "login_by_password",
    "query_qr_login_status",
    "wait_for_geetest",
]

URL_CREATE_QR_LOGIN = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/createQRLogin"
URL_QUERY_QR_LOGIN_STATUS = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/queryQRLoginStatus"
URL_LOGIN_BY_PASSWORD = "https://passport-api.mihoyo.com/account/ma-cn-passport/app/loginByPassword"
URL_COOKIE_TOKEN_BY_STOKEN = "https://passport-api.mihoyo.com/account/auth/api/getCookieAccountInfoBySToken"

APP_ID = "ddxf5dufpuyo"
PASSPORT_APP_ID = "bll8iq97cem8"
DS_SALT = "JwYDpKvLj6MrMqqYU6jTKF17KNO2PXoS"
APP_USER_AGENT = "HYPContainer/1.3.3.182"
PASSPORT_USER_AGENT = "Hyperion/550 CFNetwork/3860.500.112 Darwin/25.4.0"

PUBLIC_KEY = b"""-----BEGIN PUBLIC KEY-----
MIGfMA0GCSqGSIb3DQEBAQUAA4GNADCBiQKBgQDDvekdPMHN3AYhm/vktJT+YJr7cI5DcsNKqdsx5DZX0gDuWFuIjzdwButrIYPNmRJ1G8ybDIF7oDW2eEpm5sMbL9zs
9ExXCdvqrn51qELbqj0XxtMTIpaCHFSI50PfPpTFV9Xt/hmyVwokoOXFlAEgCn+Q
CgGs52bFoYMtyi+xEQIDAQAB
-----END PUBLIC KEY-----"""


def _random_string(length: int) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def _encrypt(value: str) -> str:
    public_key = serialization.load_pem_public_key(PUBLIC_KEY)
    encrypted = public_key.encrypt(value.encode(), padding.PKCS1v15())
    return base64.b64encode(encrypted).decode()


def _ds(body: str) -> str:
    timestamp = int(time.time())
    random_value = _random_string(6)
    digest = hashlib.md5(
        f"salt={DS_SALT}&t={timestamp}&r={random_value}&b={body}&q=".encode()
    ).hexdigest()
    return f"{timestamp},{random_value},{digest}"


def _app_headers(device_id: str) -> Dict[str, str]:
    return {
        "User-Agent": APP_USER_AGENT,
        "x-rpc-app_id": APP_ID,
        "x-rpc-client_type": "3",
        "x-rpc-device_id": device_id,
        "Content-Type": "application/json",
    }


def _passport_headers(body: str, device_id: Optional[str] = None, aigis: str = "") -> Dict[str, str]:
    return {
        "x-rpc-app_version": "2.104.0",
        "DS": _ds(body),
        "x-rpc-aigis": aigis,
        "Content-Type": "application/json",
        "Accept": "application/json",
        "x-rpc-game_biz": "bbs_cn",
        "x-rpc-sys_version": "12",
        "x-rpc-device_id": device_id or _random_string(16),
        "x-rpc-device_fp": _random_string(13),
        "x-rpc-device_name": _random_string(16),
        "x-rpc-device_model": _random_string(16),
        "x-rpc-app_id": PASSPORT_APP_ID,
        "x-rpc-client_type": "2",
        "User-Agent": PASSPORT_USER_AGENT,
    }


async def create_qr_login(
    device_id: Optional[str] = None,
) -> Tuple[BaseApiStatus, Optional[Tuple[str, str, str]]]:
    """创建米哈游启动器登录二维码，返回 (二维码 URL, ticket, device_id)。"""
    device_id = device_id or _random_string(16)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                URL_CREATE_QR_LOGIN,
                content="{}",
                headers=_app_headers(device_id),
                timeout=30,
            )
        result = response.json()
        logger.debug(f"创建米哈游扫码登录二维码: {result}")
        if result.get("retcode") == 0 and result.get("data", {}).get("url"):
            data = result["data"]
            return BaseApiStatus(success=True), (data["url"], data["ticket"], device_id)
        return BaseApiStatus(incorrect_return=True), None
    except (httpx.HTTPError, ValueError, KeyError):
        logger.exception("创建米哈游扫码登录二维码失败")
        return BaseApiStatus(network_error=True), None


async def query_qr_login_status(
    ticket: str,
    device_id: str,
) -> Tuple[BaseApiStatus, Optional[Tuple[str, Optional[BBSCookies]]]]:
    """查询二维码状态；成功时返回 (状态, Cookies)，状态为 Init/Scanned/Confirmed/Expired。"""
    body = json.dumps({"ticket": ticket}, separators=(",", ":"), ensure_ascii=False)
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                URL_QUERY_QR_LOGIN_STATUS,
                content=body,
                headers=_app_headers(device_id),
                timeout=30,
            )
        result = response.json()
        logger.debug(f"查询米哈游扫码登录状态: retcode={result.get('retcode')}, status={(result.get('data') or {}).get('status')}")
        if result.get("retcode") != 0:
            return BaseApiStatus(incorrect_return=True), ("Expired", None)

        data = result.get("data") or {}
        status = data.get("status", "Init")
        if status != "Confirmed":
            return BaseApiStatus(success=True), (status, None)

        user_info = data.get("user_info") or {}
        tokens = data.get("tokens") or []
        uid = user_info.get("aid") or user_info.get("uid") or user_info.get("account_id")
        mid = user_info.get("mid")
        token_info = next(
            (item for item in tokens if item.get("name") in {"stoken", "stoken_v2"}),
            tokens[0] if tokens else None,
        )
        token = token_info.get("token") if token_info else None
        if not (uid and mid and token):
            return BaseApiStatus(incorrect_return=True), None

        cookies = BBSCookies(mid=str(mid))
        cookies.bbs_uid = str(uid)
        cookies.stoken = token
        return BaseApiStatus(success=True), (status, cookies)
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.exception("查询米哈游扫码登录状态失败")
        return BaseApiStatus(network_error=True), None


async def get_cookie_token_by_passport_stoken(
    cookies: BBSCookies,
    device_id: Optional[str] = None,
) -> Tuple[BaseApiStatus, Optional[BBSCookies]]:
    """按 TRSS 链路通过 stoken、uid、mid 获取 cookie_token。"""
    if not (cookies.stoken and cookies.bbs_uid and cookies.mid):
        return BaseApiStatus(incorrect_return=True), None
    token = cookies.stoken
    params = {"stoken": token, "uid": cookies.bbs_uid, "mid": cookies.mid}
    cookie_header = f"stoken={token};stuid={cookies.bbs_uid};mid={cookies.mid}"
    body = ""
    headers = _passport_headers(body, device_id)
    headers["Cookie"] = cookie_header
    try:
        async with httpx.AsyncClient() as client:
            response = await client.get(
                URL_COOKIE_TOKEN_BY_STOKEN,
                params=params,
                headers=headers,
                timeout=30,
            )
        result = response.json()
        logger.debug(
            f"通过 Passport stoken 获取 cookie_token: retcode={result.get('retcode')}, message={result.get('message')}"
        )
        data = result.get("data") or {}
        if result.get("retcode") == 0 and data.get("cookie_token"):
            cookies.cookie_token = data["cookie_token"]
            if not cookies.bbs_uid and data.get("uid"):
                cookies.bbs_uid = str(data["uid"])
            return BaseApiStatus(success=True), cookies
        return BaseApiStatus(incorrect_return=True), None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.exception("通过 Passport stoken 获取 cookie_token 失败")
        return BaseApiStatus(network_error=True), None


async def login_by_password(
    account: str,
    password: str,
    aigis: str = "",
    device_id: Optional[str] = None,
) -> Tuple[BaseApiStatus, Optional[BBSCookies], Optional[Dict[str, Any]]]:
    """使用米哈游通行证账号密码登录；需要验证时第三项返回 x-rpc-aigis 数据。"""
    device_id = device_id or generate_device_id().replace("-", "")[:16]
    try:
        payload = {"account": _encrypt(account), "password": _encrypt(password)}
        body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        async with httpx.AsyncClient() as client:
            response = await client.post(
                URL_LOGIN_BY_PASSWORD,
                content=body,
                headers=_passport_headers(body, device_id, aigis),
                timeout=30,
            )
        result = response.json()
        logger.debug(
            f"米哈游账号密码登录返回: retcode={result.get('retcode')}, message={result.get('message')}"
        )
        aigis_header = response.headers.get("x-rpc-aigis")
        aigis_data = json.loads(aigis_header) if aigis_header else None

        if result.get("retcode") == -3101:
            return BaseApiStatus(need_verify=True), None, aigis_data
        if result.get("retcode") != 0:
            return BaseApiStatus(incorrect_return=True), None, {
                "retcode": result.get("retcode"),
                "message": result.get("message") or "未知错误",
            }

        data = result.get("data") or {}
        user_info = data.get("user_info") or {}
        token_info = data.get("token") or {}
        uid = user_info.get("aid") or user_info.get("uid")
        mid = user_info.get("mid")
        token = token_info.get("token")
        if not (uid and mid and token):
            return BaseApiStatus(incorrect_return=True), None, result

        cookies = BBSCookies(mid=str(mid), login_ticket=data.get("login_ticket"))
        cookies.bbs_uid = str(uid)
        cookies.stoken = token
        return BaseApiStatus(success=True), cookies, None
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        logger.exception("米哈游账号密码登录失败")
        return BaseApiStatus(network_error=True), None, None


def build_aigis_response(aigis_data: Dict[str, Any], validate: str) -> str:
    captcha_data = json.loads(aigis_data["data"])
    challenge = captcha_data["challenge"]
    payload = {
        "geetest_challenge": challenge,
        "geetest_seccode": f"{validate}|jordan",
        "geetest_validate": validate,
    }
    return f"{aigis_data['session_id']};{base64.b64encode(json.dumps(payload, separators=(',', ':')).encode()).decode()}"


async def wait_for_geetest(challenge: str, timeout: int = 300) -> Optional[str]:
    """轮询 TRSS 使用的人工验证服务，返回 geetest_validate。"""
    for _ in range(max(1, timeout // 5)):
        await asyncio.sleep(5)
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"https://challenge.minigg.cn/manual/?callback={challenge}", timeout=15
                )
            result = response.json()
            if result.get("retcode") == 200:
                return (result.get("data") or {}).get("geetest_validate")
        except (httpx.HTTPError, ValueError):
            logger.exception("查询米哈游登录人工验证结果失败")
    return None
