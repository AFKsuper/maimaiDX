import time
from re import Match

from nonebot import NoneBot, on_startup

from hoshino import Service, priv
from hoshino.typing import CQEvent, MessageSegment

from .core.arcade import (
    arcade,
    download_arcade_info,
    group_city,
    subscribe,
    updata_arcade,
    update_alias,
    update_person,
)
from .core.image import image_to_base64, text_to_image
from .log import logger as log

sv_help = """排卡指令如下：
绑定机厅城市 <城市> [城市2 ...] 群绑定机厅城市并自动订阅该市全部机厅
取消绑定城市 解除本群的机厅城市绑定
查看机厅城市 查看本群绑定的机厅城市
添加机厅 <店名> <地址> <机台数量> 添加机厅信息
删除机厅 <店名> 删除机厅信息
修改机厅 <店名> 数量 <数量> ... 修改机厅信息
添加机厅别名 <店名> <别名>
订阅机厅 <店名> 订阅机厅，简化后续指令
查看订阅 查看群组订阅机厅的信息
取消订阅机厅 <店名> 取消群组机厅订阅
查找机厅,查询机厅,机厅查找,机厅查询 <关键词> 查询对应机厅信息
<店名/别名>人数设置,设定,=,增加,加,+,减少,减,-<人数> 操作排卡人数
<店名/别名><人数> 直接上报人数，如：天河城5
<店名/别名>j 查看该店排卡明细，如：天河城j
<店名/别名>有多少人,有几人,有几卡,几人,几卡 查看排卡人数
机厅几人 查看已订阅机厅排卡人数"""

BIND_CITY_TIP = (
    "本群尚未绑定机厅城市，全国机厅同名太多、直接排卡容易冲突。\n"
    "请管理员发送：绑定机厅城市 <城市>\n"
    "例如：绑定机厅城市 广州（多个城市用空格分隔：绑定机厅城市 广州 深圳）"
)

SV_HELP = "请使用 帮助maimaiDX排卡 查看帮助"
sv = Service(
    "maimaiDX排卡", manage_priv=priv.ADMIN, enable_on_default=False, help_=SV_HELP
)


arcade_help = sv.on_fullmatch(["帮助maimaiDX排卡", "帮助maimaidx排卡"])
arcade_bind_city = sv.on_prefix(["绑定机厅城市", "设置机厅城市", "绑定机厅地区"])
arcade_unbind_city = sv.on_fullmatch(["取消绑定城市", "取消机厅城市", "取消机厅地区"])
arcade_show_city = sv.on_fullmatch(["查看机厅城市", "查看绑定城市", "本群城市"])
arcade_add = sv.on_prefix(["添加机厅", "新增机厅"])
arcade_del = sv.on_prefix(["删除机厅", "移除机厅"])
arcade_set_alias = sv.on_prefix(["添加机厅别名", "删除机厅别名"])
arcade_set = sv.on_prefix(["修改机厅", "编辑机厅"])
arcade_sub = sv.on_rex(r"^(订阅机厅|取消订阅机厅|取消订阅)\s(.+)", normalize=False)
arcade_show_sub = sv.on_fullmatch(["查看订阅", "查看订阅机厅"])
arcade_search = sv.on_prefix(
    ["查找机厅", "查询机厅", "机厅查找", "机厅查询", "搜素机厅", "机厅搜素"]
)
arcade_add_person = sv.on_rex(
    r"^(.+)?\s?(设置|设定|＝|=|增加|添加|加|＋|\+|减少|降低|减|－|-)\s?([0-9]+|＋|\+|－|-)(人|卡)?$"
)
arcade_add_person_direct = sv.on_rex(
    r"^(.+?)([0-9]+)(人|卡)?$", normalize=False
)
arcade_quick_detail = sv.on_rex(r"^(.+?)j$", normalize=False)
arcade_person_num = sv.on_fullmatch(["机厅几人", "jtj"])
arcade_person_num_2 = sv.on_suffix(
    ["有多少人", "有几人", "有几卡", "多少人", "多少卡", "几人", "jr", "几卡"]
)


@on_startup
async def _():
    log.info("正在获取maimai所有机厅信息")
    await arcade.get_arcade()
    log.info("maimai机厅数据获取完成")


@arcade_help
async def _(bot: NoneBot, ev: CQEvent):
    await bot.send(
        ev,
        MessageSegment.image(image_to_base64(text_to_image(sv_help))),
        at_sender=True,
    )


@arcade_bind_city
async def _(bot: NoneBot, ev: CQEvent):
    args: str = ev.message.extract_plain_text().strip()
    gid = ev.group_id
    if not priv.check_priv(ev, priv.ADMIN):
        msg = "仅允许管理员绑定机厅城市"
    elif not args:
        msg = "格式错误：绑定机厅城市 <城市>，例如：绑定机厅城市 广州"
    else:
        keys = [
            k.strip().removesuffix("市")
            for k in args.replace(",", " ").replace("，", " ").split()
            if k.strip().removesuffix("市")
        ]
        if not keys:
            msg = "格式错误：绑定机厅城市 <城市>，例如：绑定机厅城市 广州"
        else:
            missed = [k for k in keys if not arcade.total.search_city(k)]
            if missed:
                msg = (
                    "以下城市在机厅数据中未找到机厅，请检查城市名："
                    + "、".join(missed)
                    + "\n可发送 查找机厅 <关键词> 检查机厅数据"
                )
            else:
                city = " ".join(keys)
                await group_city.set(gid, city)
                city_arcades = arcade.total.search_city(city)
                city_ids = {a.id for a in city_arcades}
                # 换绑时退订不在新城市范围内的机厅，保持订阅与绑定城市一致
                unsub = 0
                for a in arcade.total.group_subscribe_arcade(gid):
                    if a.id not in city_ids:
                        a.group.remove(gid)
                        unsub += 1
                # 默认订阅该城市全部机厅
                added = 0
                for a in city_arcades:
                    if gid not in a.group:
                        a.group.append(gid)
                        added += 1
                if added or unsub:
                    await arcade.total.save_arcade()
                msg = (
                    f"本群已绑定机厅城市：{city}\n"
                    f"该地区共 {len(city_arcades)} 家机厅，已全部默认订阅"
                    f"（新增 {added} 家，退订非本地区 {unsub} 家）\n"
                    f"发送 jtj 查看已初始化店铺数量，<店名>有几人 查看单店详情"
                )
    await bot.send(ev, msg, at_sender=True)


@arcade_unbind_city
async def _(bot: NoneBot, ev: CQEvent):
    gid = ev.group_id
    if not priv.check_priv(ev, priv.ADMIN):
        msg = "仅允许管理员取消绑定机厅城市"
    elif not group_city.get(gid):
        msg = "本群未绑定机厅城市，无需取消"
    else:
        await group_city.remove(gid)
        msg = "本群已取消机厅城市绑定，排卡将恢复全国范围（不建议）"
    await bot.send(ev, msg, at_sender=True)


@arcade_show_city
async def _(bot: NoneBot, ev: CQEvent):
    city = group_city.get(ev.group_id)
    if city:
        count = len(arcade.total.search_city(city))
        msg = f"本群绑定的机厅城市：{city}\n该地区共 {count} 家机厅"
    else:
        msg = BIND_CITY_TIP
    await bot.send(ev, msg, at_sender=True)


@arcade_add
async def _(bot: NoneBot, ev: CQEvent):
    args: list[str] = ev.message.extract_plain_text().strip().split()
    if not priv.check_priv(ev, priv.SUPERUSER):
        msg = "仅允许主人添加机厅\n请使用 来杯咖啡+内容 联系主人"
    elif len(args) == 1 and args[0] in ["帮助", "help", "指令帮助"]:
        msg = "添加机厅指令格式：添加机厅 <店名> <位置> <机台数量> <别称1> <别称2> ..."
    elif len(args) >= 3:
        if not args[2].isdigit():
            msg = "格式错误：添加机厅 <店名> <地址> <机台数量> [别称1] [别称2] ..."
        else:
            if not arcade.total.search_fullname(args[0]):
                # idList 是启动时的快照、不含刚添加的机厅，直接按当前数据生成唯一 id，
                # 否则连续添加两家会都得到 10000，订阅/改人数按 id 会命中错店
                used = {int(a.id) for a in arcade.total}
                sid = 10000
                while sid in used:
                    sid += 1
                arcade_dict = {
                    "name": args[0],
                    "location": args[1],
                    "province": "",
                    "mall": "",
                    "num": int(args[2]) if len(args) > 2 else 1,
                    "id": str(sid),
                    "alias": args[3:] if len(args) > 3 else [],
                    "group": [],
                    "person": 0,
                    "by": "",
                    "time": "",
                }
                arcade.total.add_arcade(arcade_dict)
                await arcade.total.save_arcade()
                msg = f"机厅：{args[0]} 添加成功"
            else:
                msg = f"机厅：{args[0]} 已存在，无法添加机厅"
    else:
        msg = "格式错误：添加机厅 <店名> <地址> <机台数量> [别称1] [别称2] ..."

    await bot.send(ev, msg, at_sender=True)


@arcade_del
async def _(bot: NoneBot, ev: CQEvent):
    name: str = ev.message.extract_plain_text().strip()
    if not priv.check_priv(ev, priv.SUPERUSER):
        msg = "仅允许主人删除机厅\n请使用 来杯咖啡+内容 联系主人"
    elif not name:
        msg = "格式错误：删除机厅 <店名>，店名需全名"
    else:
        if not arcade.total.search_fullname(name):
            msg = f"未找到机厅：{name}"
        else:
            arcade.total.del_arcade(name)
            await arcade.total.save_arcade()
            msg = f"机厅：{name} 删除成功"
    await bot.send(ev, msg, at_sender=True)


@arcade_set_alias
async def _(bot: NoneBot, ev: CQEvent):
    args: list[str] = ev.message.extract_plain_text().strip().split()
    a = True if ev.prefix == "添加机厅别名" else False
    if len(args) != 2:
        msg = "格式错误：添加/删除机厅别名 <店名> <别名>"
    elif (
        not args[0].isdigit() and len(_arc := arcade.total.search_fullname(args[0])) > 1
    ):
        msg = "找到多个相同店名的机厅，请使用店铺ID更改机厅别名\n" + "\n".join(
            [f"{_.id}：{_.name}" for _ in _arc]
        )
    else:
        msg = await update_alias(args[0], args[1], a)
    await bot.send(ev, msg, at_sender=True)


@arcade_set
async def _(bot: NoneBot, ev: CQEvent):
    args: list[str] = ev.message.extract_plain_text().strip().split()
    if not priv.check_priv(ev, priv.ADMIN):
        msg = "仅允许管理员修改机厅信息"
    elif len(args) != 3 or args[1] != "数量" or not args[2].isdigit():
        msg = "格式错误：修改机厅 <店名/ID> 数量 <数量>"
    elif (
        not args[0].isdigit() and len(_arc := arcade.total.search_fullname(args[0])) > 1
    ):
        msg = "找到多个相同店名的机厅，请使用店铺ID修改机厅\n" + "\n".join(
            [f"{_.id}：{_.name}" for _ in _arc]
        )
    else:
        msg = await updata_arcade(args[0], args[2])

    await bot.send(ev, msg, at_sender=True)


@arcade_sub
async def _(bot: NoneBot, ev: CQEvent):
    match: Match[str] = ev["match"]
    gid = ev.group_id
    sub = True if match.group(1) == "订阅机厅" else False
    name = match.group(2)
    city = group_city.get(gid)
    if not priv.check_priv(ev, priv.ADMIN):
        msg = "仅允许管理员订阅和取消订阅"
    elif not city and sub:
        msg = BIND_CITY_TIP
    elif not sub:
        # 取消订阅只在本群已订阅的机厅中找，避免换绑城市后旧订阅无法取消
        subscribed = arcade.total.group_subscribe_arcade(gid)
        _arc = [
            a
            for a in subscribed
            if (a.id == name if name.isdigit() else a.name == name)
        ]
        if not _arc:
            msg = f"该群未订阅机厅：{name}"
        else:
            msg = await subscribe(gid, _arc[0].id, sub)
    else:
        _arc = (
            arcade.total.search_id(name, city)
            if name.isdigit()
            else arcade.total.search_fullname(name, city)
        )
        if not _arc:
            msg = (
                f"本群绑定城市（{city}）下未找到机厅：{name}\n"
                f"可发送 查找机厅 {name} 查看该城市的机厅"
            )
        elif len(_arc) > 1:
            msg = "本群绑定城市下找到多个相同店名的机厅，请使用店铺ID订阅\n" + "\n".join(
                [f"{_.id}：{_.name}" for _ in _arc]
            )
        else:
            msg = await subscribe(gid, _arc[0].id, sub, city)

    await bot.send(ev, msg, at_sender=True)


@arcade_show_sub
async def _(bot: NoneBot, ev: CQEvent):
    gid = int(ev.group_id)
    arcadeList = arcade.total.group_subscribe_arcade(group_id=gid)
    if arcadeList:
        result = [f"群{gid}订阅机厅信息如下："]
        for a in arcadeList:
            alias = "\n  ".join(a.alias)
            result.append(f"""店名：{a.name}
    - 地址：{a.location}
    - 数量：{a.num}
    - 别名：{alias}""")
        msg = "\n".join(result)
    else:
        msg = "该群未订阅任何机厅"
    await bot.send(ev, msg, at_sender=True)


@arcade_search
async def _(bot: NoneBot, ev: CQEvent):
    name: str = ev.message.extract_plain_text().strip()
    city = group_city.get(ev.group_id)
    if not name:
        await bot.finish(ev, "格式错误：查找机厅 <关键词>", at_sender=True)
    elif not city:
        await bot.finish(ev, BIND_CITY_TIP, at_sender=True)
    elif arcade_list := arcade.total.search_name(name, city):
        result = [f"本群绑定城市（{city}）内为您找到以下机厅：\n"]
        for a in arcade_list:
            result.append(f"""店名：{a.name}
    - 地址：{a.location}
    - ID：{a.id}
    - 数量：{a.num}""")
        if len(arcade_list) < 5:
            await bot.send(ev, "\n==========\n".join(result), at_sender=True)
        else:
            await bot.send(
                ev,
                MessageSegment.image(image_to_base64(text_to_image("\n".join(result)))),
                at_sender=True,
            )
    else:
        await bot.send(ev, "没有这样的机厅哦", at_sender=True)


@arcade_add_person
async def _(bot: NoneBot, ev: CQEvent):
    if not group_city.get(ev.group_id):
        await bot.send(ev, BIND_CITY_TIP, at_sender=True)
        return
    try:
        match: Match[str] = ev["match"]
        gid = ev.group_id
        nickname = ev.sender["nickname"]
        if not match.group(3).isdigit() and match.group(3) not in [
            "＋",
            "+",
            "－",
            "-",
        ]:
            await bot.finish(ev, "请输入正确的数字", at_sender=True)
        arcade_list = arcade.total.group_subscribe_arcade(group_id=gid)
        if not arcade_list:
            await bot.finish(ev, "该群未订阅机厅，无法更改机厅人数", at_sender=True)
        value = match.group(2)
        _amount = match.group(3)
        person = 1 if _amount in ["＋", "+", "－", "-"] else int(_amount)
        if match.group(1):
            if "人数" in match.group(1) or "卡" in match.group(1):
                arcadeName = (
                    match.group(1)[:-2]
                    if "人数" in match.group(1)
                    else match.group(1)[:-1]
                )
            else:
                arcadeName = match.group(1)
            _arcade = []
            for _a in arcade_list:
                if arcadeName == _a.name:
                    _arcade.append(_a)
                    break
                if arcadeName in _a.alias:
                    _arcade.append(_a)
                    break
            if not _arcade:
                msg = "已订阅的机厅中未找到该机厅"
            else:
                msg = await update_person(_arcade, nickname, value, person)

            await bot.send(ev, msg, at_sender=True)
    except Exception:
        pass


@arcade_add_person_direct
async def _(bot: NoneBot, ev: CQEvent):
    """店名+数字直接上报人数，如：天河城5（设置为 5 人）"""
    try:
        match: Match[str] = ev["match"]
        gid = ev.group_id
        if not group_city.get(gid):
            return
        if not match.group(2).isdigit():
            return
        arcadeName = match.group(1).strip()
        if arcadeName.endswith("人数"):
            arcadeName = arcadeName[:-2]
        elif arcadeName.endswith("卡"):
            arcadeName = arcadeName[:-1]
        if not arcadeName:
            return
        arcade_list = arcade.total.group_subscribe_arcade(group_id=gid)
        if not arcade_list:
            return
        _arcade = []
        key = arcadeName.lower()
        for _a in arcade_list:
            if key == _a.name.lower() or any(key == al.lower() for al in _a.alias):
                _arcade.append(_a)
                break
        if not _arcade:
            # 不是排卡上报指令，静默忽略，避免普通消息误触发
            log.info(f"直写上报「{arcadeName}{match.group(2)}」未匹配本群订阅机厅（群{gid}），已忽略")
            return
        nickname = ev.sender["nickname"]
        msg = await update_person(_arcade, nickname, "=", int(match.group(2)))
        await bot.send(ev, msg, at_sender=True)
    except Exception:
        pass


@arcade_quick_detail
async def _(bot: NoneBot, ev: CQEvent):
    """店名+j 快捷查看单店排卡明细，如：天河城j"""
    try:
        match: Match[str] = ev["match"]
        gid = ev.group_id
        if not group_city.get(gid):
            return
        arcadeName = match.group(1).strip()
        if not arcadeName:
            return
        arcade_list = arcade.total.group_subscribe_arcade(group_id=gid)
        if not arcade_list:
            return
        _arcade = []
        key = arcadeName.lower()
        for _a in arcade_list:
            if key == _a.name.lower() or any(key == al.lower() for al in _a.alias):
                _arcade.append(_a)
                break
        if not _arcade:
            # 不是查排卡指令，静默忽略，避免普通消息误触发
            log.info(f"单店明细「{arcadeName}j」未匹配本群订阅机厅（群{gid}），已忽略")
            return
        result = arcade.total.arcade_to_msg(_arcade)
        await bot.send(ev, "\n".join(result), at_sender=True)
    except Exception:
        pass


@arcade_person_num
async def _(bot: NoneBot, ev: CQEvent):
    gid = ev.group_id
    if not group_city.get(gid):
        await bot.finish(ev, BIND_CITY_TIP, at_sender=True)
    arcade_list = arcade.total.group_subscribe_arcade(gid)
    if arcade_list:
        # 初始化（已订阅）的机厅只报总数；有人上报的机厅仍列出明细
        reported = [
            a
            for a in arcade_list
            if a.person != 0 or (a.by and a.by != "自动清零")
        ]
        lines = [f"本群共 {len(arcade_list)} 家店铺已初始化"]
        if reported:
            lines += arcade.total.arcade_to_msg(reported)
        else:
            lines.append("暂无机厅有人上报")
        await bot.send(ev, "\n".join(lines), at_sender=True)
    else:
        await bot.finish(ev, "该群未订阅任何机厅", at_sender=True)


@arcade_person_num_2
async def _(bot: NoneBot, ev: CQEvent):
    gid = ev.group_id
    city = group_city.get(gid)
    name = ev.message.extract_plain_text().strip().lower()
    result = None
    if not city:
        await bot.finish(ev, BIND_CITY_TIP, at_sender=True)
    if name:
        arcade_list = arcade.total.search_name(name, city)
        if not arcade_list:
            await bot.finish(
                ev,
                f"本群绑定城市（{city}）内没有这样的机厅哦",
                at_sender=True,
            )
        result = arcade.total.arcade_to_msg(arcade_list)
        await bot.send(ev, "\n".join(result))
    else:
        arcade_list = arcade.total.group_subscribe_arcade(gid)
        if arcade_list:
            result = arcade.total.arcade_to_msg(arcade_list)
            await bot.send(ev, "\n".join(result))
        else:
            await bot.send(
                ev,
                "该群未订阅任何机厅，请使用 订阅机厅 <名称> 指令订阅机厅",
                at_sender=True,
            )


@sv.scheduled_job("cron", hour="4")
async def _():
    try:
        await download_arcade_info()
        for _ in arcade.total:
            _.person = 0
            _.by = "自动清零"
            _.time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        await arcade.total.save_arcade()
    except Exception:
        return
    log.info("maimaiDX排卡数据更新完毕")
