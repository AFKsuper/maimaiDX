from textwrap import dedent

from httpx import HTTPError as HTTPXError
from maimai_py.exceptions import (
    AimeServerError,
    ArcadeError,
    ArcadeIdentifierError,
    InvalidDeveloperTokenError,
    InvalidPlayerIdentifierError,
    PrivacyLimitationError,
    TitleServerBlockedError,
    TitleServerNetworkError,
)
from nonebot import NoneBot

from hoshino.typing import CQEvent

from ..config import log, sv
from ..core.database.qq import clear_upload_credentials, update_user
from ..core.merge.models import ServiceName
from ..core.upload import (
    QRCODE_PREFIX,
    QRCodeFormatError,
    UploadCredentialNotFoundError,
    upload_scores,
)
from .depend import GetOrCreateSender

PRIVATE_ONLY_MSG = (
    "凭据属于您的私人信息，请添加 BOT 为好友后在私聊中发送该指令。\n"
    "如果凭据已经发到群里，请立刻到查分器重新生成一份。"
)
QRCODE_GUIDE = dedent(f"""
    请在指令后附上舞萌二维码内容，例如：
    上传成绩 {QRCODE_PREFIX}xxxxxxxx

    二维码内容可在「舞萌 DX」微信服务号或 NFC 读卡工具中获取，
    有效期只有几分钟，过期后请重新获取。
""").strip()
DIVINGFISH_TOKEN_GUIDE = dedent("""
    未配置水鱼导入 Token，无法上传成绩。
    请前往 https://www.diving-fish.com/maimaidx/prober/ 登录后，
    在「编辑个人资料」中复制「导入 Token」，
    再私聊 BOT 发送「水鱼导入token <您的Token>」。
""").strip()
LXNS_TOKEN_GUIDE = dedent("""
    未配置落雪上传凭据，无法上传成绩。
    请前往 https://maimai.lxns.net/user/profile 复制「个人 API 密钥」，
    再私聊 BOT 发送「落雪个人密钥 <您的密钥>」。
""").strip()
CREDENTIAL_GUIDE = {
    ServiceName.DIVINGFISH: DIVINGFISH_TOKEN_GUIDE,
    ServiceName.LXNS: LXNS_TOKEN_GUIDE,
}

upload = sv.on_prefix(["上传成绩", "上传分数", "传分"])
dftoken = sv.on_prefix(["水鱼导入token", "水鱼导入Token", "dftoken"])
lxtoken = sv.on_prefix(["落雪个人密钥", "落雪上传密钥", "lxtoken"])
delete_token = sv.on_fullmatch(["删除上传凭据", "清除上传凭据"])


def is_private(ev: CQEvent) -> bool:
    return ev.detail_type == "private"


@upload
async def _(bot: NoneBot, ev: CQEvent):
    user = await GetOrCreateSender(bot, ev)
    qrcode = ev.message.extract_plain_text().strip()
    if not qrcode:
        await bot.finish(ev, QRCODE_GUIDE, at_sender=True)

    try:
        result = await upload_scores(user, qrcode)
    except QRCodeFormatError:
        await bot.finish(ev, QRCODE_GUIDE, at_sender=True)
    except UploadCredentialNotFoundError as error:
        await bot.finish(ev, CREDENTIAL_GUIDE[error.service], at_sender=True)
    except ArcadeIdentifierError:
        await bot.finish(
            ev, "二维码无效或已过期，请重新获取二维码后再试。", at_sender=True
        )
    except AimeServerError:
        await bot.finish(
            ev, "舞萌 Aime 服务器拒绝了该二维码，请重新获取后再试。", at_sender=True
        )
    except TitleServerBlockedError:
        log.error("舞萌 title 服务器拒绝了本机请求，可能需要配置国内代理")
        await bot.finish(
            ev,
            "舞萌服务器拒绝了本 BOT 的请求，请联系 BOT 管理员检查服务器网络环境。",
            at_sender=True,
        )
    except (TitleServerNetworkError, ArcadeError, HTTPXError) as error:
        log.warning(f"上传成绩时网络异常：{type(error).__name__}")
        await bot.finish(ev, "连接舞萌服务器失败，请稍后再试。", at_sender=True)
    except InvalidPlayerIdentifierError:
        await bot.finish(
            ev,
            "查分器拒绝了上传凭据，请确认凭据未过期，必要时重新绑定。",
            at_sender=True,
        )
    except InvalidDeveloperTokenError:
        log.error("查分器开发者Token异常，无法上传成绩")
        await bot.finish(
            ev, "查分器开发者Token异常，请联系 BOT 管理员检查配置。", at_sender=True
        )
    except PrivacyLimitationError:
        await bot.finish(
            ev,
            "查分器隐私设置不允许本 BOT 写入成绩，请在查分器中开放权限。",
            at_sender=True,
        )

    await bot.send(
        ev,
        f"成绩上传完成，已向「{result.service.value}」写入 {result.count} 条成绩。",
        at_sender=True,
    )


@dftoken
async def _(bot: NoneBot, ev: CQEvent):
    if not is_private(ev):
        await bot.finish(ev, PRIVATE_ONLY_MSG, at_sender=True)
    user = await GetOrCreateSender(bot, ev)
    token = ev.message.extract_plain_text().strip()
    if not token:
        await bot.finish(ev, DIVINGFISH_TOKEN_GUIDE, at_sender=True)

    await update_user(user.qqid, divingfish_import_token=token)
    await bot.send(
        ev,
        "已保存水鱼导入 Token，数据源为水鱼时可使用「上传成绩」指令传分。",
        at_sender=True,
    )


@lxtoken
async def _(bot: NoneBot, ev: CQEvent):
    if not is_private(ev):
        await bot.finish(ev, PRIVATE_ONLY_MSG, at_sender=True)
    user = await GetOrCreateSender(bot, ev)
    token = ev.message.extract_plain_text().strip()
    if not token:
        await bot.finish(ev, LXNS_TOKEN_GUIDE, at_sender=True)

    await update_user(user.qqid, lxns_personal_token=token)
    await bot.send(
        ev,
        "已保存落雪个人 API 密钥，数据源为落雪时可使用「上传成绩」指令传分。",
        at_sender=True,
    )


@delete_token
async def _(bot: NoneBot, ev: CQEvent):
    user = await GetOrCreateSender(bot, ev)
    cleared = await clear_upload_credentials(user.qqid)
    if cleared:
        await bot.send(ev, "已删除您保存的全部上传凭据。", at_sender=True)
    else:
        await bot.send(ev, "您没有保存任何上传凭据。", at_sender=True)
