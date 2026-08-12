import asyncio
import json
import math
from typing import Dict, Optional, Union

from nonebot import on_command
from nonebot.adapters.onebot.v11 import MessageEvent as OneBotV11MessageEvent, MessageSegment as OneBotV11MessageSegment
from nonebot.adapters.qq import MessageSegment as QQGuildMessageSegment, DirectMessageCreateEvent, \
    MessageEvent as QQGuildMessageEvent
from nonebot.adapters.qq.exception import AuditException
from nonebot.exception import ActionFailed
from nonebot.internal.matcher import Matcher
from nonebot.internal.params import ArgStr
from nonebot.params import Arg, T_State

from ..api.common import get_ltoken_by_stoken, get_cookie_token_by_stoken, get_device_fp
from ..api.passport_login import build_aigis_response, create_qr_login, \
    get_cookie_token_by_passport_stoken, login_by_password, query_qr_login_status, wait_for_geetest
from ..command.common import CommandRegistry
from ..model import PluginDataManager, plugin_config, UserAccount, UserData, CommandUsage, BBSCookies
from ..utils import logger, COMMAND_BEGIN, GeneralMessageEvent, GeneralPrivateMessageEvent, \
    GeneralGroupMessageEvent, read_blacklist, read_whitelist, generate_device_id, generate_qr_img

__all__ = ["get_cookie", "password_login", "output_cookies"]

get_cookie = on_command(plugin_config.preference.command_start + "登录", priority=4, block=True)
password_login = on_command(plugin_config.preference.command_start + "账密登录", priority=4, block=True)
_login_locks: Dict[str, asyncio.Lock] = {}

CommandRegistry.set_usage(
    get_cookie,
    CommandUsage(
        name="登录",
        description="使用米游社 App 扫描米哈游启动器二维码，绑定米游社账户。",
    ),
)
CommandRegistry.set_usage(
    password_login,
    CommandUsage(
        name="账密登录",
        description="使用米哈游通行证账号和密码绑定米游社账户；请务必私聊使用。",
    ),
)


def _login_lock(event: GeneralMessageEvent) -> asyncio.Lock:
    return _login_locks.setdefault(event.get_user_id(), asyncio.Lock())


def _release_login_lock(user_id: Optional[str]):
    if not user_id:
        return
    lock = _login_locks.get(user_id)
    if lock and lock.locked():
        lock.release()


def _check_user_access(event: GeneralMessageEvent) -> Optional[str]:
    if plugin_config.preference.enable_blacklist and event.get_user_id() in read_blacklist():
        return "⚠️您已被加入黑名单，无法使用本功能"
    if plugin_config.preference.enable_whitelist and event.get_user_id() not in read_whitelist():
        return "⚠️您不在白名单内，无法使用本功能"
    users = set(PluginDataManager.plugin_data.users.values())
    user_id = event.get_user_id()
    if (
        user_id not in PluginDataManager.plugin_data.users
        and plugin_config.preference.max_user not in [-1, 0]
        and len(users) >= plugin_config.preference.max_user
    ):
        return "⚠️目前可支持使用用户数已经满啦~"
    return None


def _get_user(event: GeneralMessageEvent) -> UserData:
    user_id = event.get_user_id()
    PluginDataManager.plugin_data.users.setdefault(user_id, UserData())
    user = PluginDataManager.plugin_data.users[user_id]
    if isinstance(event, DirectMessageCreateEvent):
        user.qq_guild[user_id] = event.channel_id
    return user


async def _save_account(event: GeneralMessageEvent, cookies: BBSCookies, device_id: str) -> bool:
    user_id = event.get_user_id()
    user = _get_user(event)
    bbs_uid = cookies.bbs_uid
    if not bbs_uid:
        return False

    account = user.accounts.get(bbs_uid)
    account_device_id = account.device_id_ios if account else generate_device_id()
    staged = account.cookies.copy(deep=True) if account and account.cookies else BBSCookies()
    # 只合并新链路实际返回的字段，避免补全失败时清空旧的有效 Cookie。
    staged.update(cookies.dict(exclude_none=True))
    staged.bbs_uid = bbs_uid
    fp_status, device_fp = await get_device_fp(account_device_id)

    # 使用新扫码/账密接口返回的 stoken_v2 继续补齐 ltoken 与 cookie_token。
    ltoken_status, updated = await get_ltoken_by_stoken(staged, account_device_id)
    if ltoken_status and updated:
        staged.update(updated.dict(exclude_none=True))
    cookie_status, updated = await get_cookie_token_by_passport_stoken(
        staged, account_device_id.replace("-", "")[:16]
    )
    if cookie_status and updated:
        staged.update(updated.dict(exclude_none=True))
    else:
        # 保留 mystool 原有实现作为兼容兜底；主链路与 TRSS 保持一致。
        cookie_status, updated = await get_cookie_token_by_stoken(staged, account_device_id)
        if cookie_status and updated:
            staged.update(updated.dict(exclude_none=True))

    if not staged.is_correct():
        logger.warning(f"米游社账户 {bbs_uid} 登录成功，但 Cookies 未补齐；保留原有账户数据")
        return False

    if not account:
        account = UserAccount(
            phone_number=None,
            cookies=staged,
            device_id_ios=account_device_id,
            device_id_android=generate_device_id(),
        )
        user.accounts[bbs_uid] = account
    else:
        account.cookies = staged
    if fp_status:
        account.device_fp = device_fp
    PluginDataManager.write_plugin_data()
    logger.success(f"{plugin_config.preference.log_head}米游社账户 {bbs_uid} 绑定成功")
    return True


async def _send_qr(matcher: Matcher, event: GeneralMessageEvent, url: str):
    image_bytes = generate_qr_img(url)
    if isinstance(event, OneBotV11MessageEvent):
        image = OneBotV11MessageSegment.image(image_bytes)
    elif isinstance(event, QQGuildMessageEvent):
        image = QQGuildMessageSegment.file_image(image_bytes)
    else:
        await matcher.finish("⚠️当前平台不支持发送登录二维码")
        return
    try:
        await matcher.send(image)
    except (ActionFailed, AuditException):
        logger.exception("发送米哈游登录二维码失败")
        await matcher.finish("⚠️发送二维码失败，无法登录")


@get_cookie.handle()
async def handle_qr_login(event: Union[GeneralMessageEvent]):
    if isinstance(event, GeneralGroupMessageEvent):
        await get_cookie.finish("⚠️为了保护您的账户安全，请私聊使用『/登录』。")
    error = _check_user_access(event)
    if error:
        await get_cookie.finish(error)
    _get_user(event)
    lock = _login_lock(event)
    if lock.locked():
        await get_cookie.finish("⚠️当前已有正在进行的登录，请完成后再试。")

    async with lock:
        status, result = await create_qr_login()
        if not status or not result:
            await get_cookie.finish("⚠️创建米哈游登录二维码失败，请稍后重试。")
        qrcode_url, ticket, device_id = result
        await get_cookie.send("请使用米游社 App 扫描下面的米哈游启动器登录二维码，并在 App 内确认登录。")
        await _send_qr(get_cookie, event, qrcode_url)

        interval = max(float(plugin_config.preference.qrcode_query_interval), 0.1)
        wait_time = max(float(plugin_config.preference.qrcode_wait_time), interval)
        query_times = max(1, math.ceil(wait_time / interval))
        scanned = False
        cookies = None
        for _ in range(query_times):
            await asyncio.sleep(interval)
            status, result = await query_qr_login_status(ticket, device_id)
            if not status or not result:
                if status.incorrect_return:
                    await get_cookie.finish("⚠️二维码已过期或登录请求失效，请重新发送『/登录』。")
                continue
            qr_status, cookies = result
            if qr_status == "Scanned" and not scanned:
                scanned = True
                await get_cookie.send("二维码已扫描，请在米游社 App 内确认登录。")
            if qr_status == "Confirmed" and cookies:
                break
        else:
            await get_cookie.finish("⚠️等待扫码确认超时，请重新发送『/登录』。")

        if not cookies or not await _save_account(event, cookies, device_id):
            await get_cookie.finish("⚠️扫码登录成功，但获取完整 Cookies 失败；原有账户数据未被覆盖。")
        await get_cookie.finish(f"🎉米游社账户 {cookies.bbs_uid} 绑定成功")


@password_login.handle()
async def handle_password_login(event: Union[GeneralMessageEvent], state: T_State):
    if isinstance(event, GeneralGroupMessageEvent):
        await password_login.finish("⚠️账号和密码属于敏感信息，请私聊使用『/账密登录』。")
    error = _check_user_access(event)
    if error:
        await password_login.finish(error)
    _get_user(event)
    lock = _login_lock(event)
    if lock.locked():
        await password_login.finish("⚠️当前已有正在进行的登录，请完成后再试。")
    await lock.acquire()
    state["login_lock_user_id"] = event.get_user_id()
    await password_login.send("请输入米哈游通行证账号（手机号或邮箱）。发送“退出”可取消。")


@password_login.got("account")
async def receive_account(matcher: Matcher, state: T_State, account: str = ArgStr()):
    account = account.strip()
    state["account"] = account
    if account == "退出":
        _release_login_lock(state.get("login_lock_user_id"))
        await matcher.finish("已取消账密登录。")
    if not account:
        await matcher.reject("账号不能为空，请重新输入。")
    if len(account.encode()) > 117:
        await matcher.reject("账号内容过长，请重新输入手机号或邮箱。")
    await matcher.send("请输入米哈游通行证密码。登录完成后建议撤回本消息；发送“退出”可取消。")


@password_login.got("password")
async def receive_password(
    event: Union[GeneralPrivateMessageEvent],
    matcher: Matcher,
    state: T_State,
    password: str = ArgStr(),
    account: str = Arg("account"),
):
    password = password.strip()
    lock_user_id = state.get("login_lock_user_id")
    try:
        if password == "退出":
            await matcher.finish("已取消账密登录。")
        if not password or len(password.encode()) > 117:
            await matcher.finish("⚠️密码为空或内容过长，请重新发送『/账密登录』。")
        account = account.strip()
        device_id = generate_device_id().replace("-", "")[:16]
        status, cookies, detail = await login_by_password(account, password, device_id=device_id)

        if status.need_verify and detail:
            try:
                captcha_data = json.loads(detail["data"])
                challenge = captcha_data["challenge"]
                gt = captcha_data["gt"]
            except (KeyError, TypeError, ValueError):
                await matcher.finish("⚠️米哈游要求安全验证，但验证参数解析失败。")
            await matcher.send(
                "米哈游要求进行安全验证。请在 5 分钟内打开下面的链接完成验证：\n"
                f"https://challenge.minigg.cn/manual/index.html?gt={gt}&challenge={challenge}"
            )
            validate = await wait_for_geetest(challenge)
            if not validate:
                await matcher.finish("⚠️安全验证超时，请重新发送『/账密登录』。")
            try:
                aigis = build_aigis_response(detail, validate)
            except (KeyError, TypeError, ValueError):
                await matcher.finish("⚠️安全验证参数无效，请重新发送『/账密登录』。")
            status, cookies, detail = await login_by_password(
                account, password, aigis=aigis, device_id=device_id
            )

        if not status or not cookies:
            message = detail.get("message") if isinstance(detail, dict) else None
            await matcher.finish(f"⚠️账密登录失败：{message or '网络错误或米哈游服务器返回异常'}")
        if not await _save_account(event, cookies, device_id):
            await matcher.finish("⚠️账号密码验证成功，但获取完整 Cookies 失败；原有账户数据未被覆盖。")
        await matcher.finish(f"🎉米游社账户 {cookies.bbs_uid} 绑定成功")
    finally:
        _release_login_lock(lock_user_id)


output_cookies = on_command(
    plugin_config.preference.command_start + '导出Cookies',
    aliases={plugin_config.preference.command_start + '导出Cookie', plugin_config.preference.command_start + '导出账号',
             plugin_config.preference.command_start + '导出cookie',
             plugin_config.preference.command_start + '导出cookies'}, priority=4,
    block=True)

CommandRegistry.set_usage(
    output_cookies,
    CommandUsage(
        name="导出Cookies",
        description="导出绑定的米游社账号的Cookies数据"
    )
)


@output_cookies.handle()
async def handle_first_receive(event: Union[GeneralMessageEvent], state: T_State):
    """
    Cookies导出命令触发
    """
    if isinstance(event, GeneralGroupMessageEvent):
        await output_cookies.finish("⚠️为了保护您的隐私，请私聊进行Cookies导出。")
    user_account = PluginDataManager.plugin_data.users[event.get_user_id()].accounts
    if not user_account:
        await output_cookies.finish(f"⚠️你尚未绑定米游社账户，请先使用『{COMMAND_BEGIN}登录』进行登录")
    elif len(user_account) == 1:
        account = next(iter(user_account.values()))
        state["bbs_uid"] = account.bbs_uid
    else:
        msg = "您有多个账号，您要导出哪个账号的Cookies数据？\n"
        msg += "\n".join(map(lambda x: f"🆔{x}", user_account))
        msg += "\n🚪发送“退出”即可退出"
        await output_cookies.send(msg)


@output_cookies.got('bbs_uid')
async def _(event: Union[GeneralPrivateMessageEvent], matcher: Matcher, bbs_uid=ArgStr()):
    """
    根据手机号设置导出相应的账户的Cookies
    """
    if bbs_uid == '退出':
        await matcher.finish('🚪已成功退出')
    user_account = PluginDataManager.plugin_data.users[event.get_user_id()].accounts
    if bbs_uid in user_account:
        await output_cookies.finish(json.dumps(user_account[bbs_uid].cookies.dict(cookie_type=True), indent=4))
    else:
        await matcher.reject('⚠️您输入的账号不在以上账号内，请重新输入')
