import json
import re
import time
import traceback

import httpx
from pydantic import BaseModel

from ..config import log
from ..resources import arcades_json, arcade_city_json
from .tool import writefile


class Arcade(BaseModel):
    
    name: str
    location: str
    province: str
    mall: str
    num: int
    id: str
    alias: list[str]
    group: list[int]
    person: int
    by: str
    time: str


def match_city(arcade: Arcade, city: str) -> bool:
    """机厅是否属于指定城市。

    `city` 可为多个城市，用空格或逗号分隔（如 `广州 深圳`），
    任一城市命中即算归属；`city` 为空表示不限定城市。
    官方数据没有城市字段，城市名需在地址/店名/商场/省份中命中。
    地址/商场字段必须命中「XX市」完整形式，避免"南昌路（在洛阳）"这类
    路名和官方脏 mall 字段（曾把别家店名写进 mall）误判；
    店名/省份允许裸城市名命中（如"环游嘉年华广州天河城店"）。
    """
    if not city:
        return True
    keys = [
        k.removesuffix("市").lower()
        for k in city.replace(",", " ").replace("，", " ").split()
        if k.removesuffix("市")
    ]
    if not keys:
        return True
    # 地址/商场只认「城市+市」的完整行政区划写法
    strict_fields = [arcade.location, arcade.mall]
    if any(
        k + "市" in f.lower() for k in keys for f in strict_fields
    ):
        return True
    # 兜底：地址中紧跟省/自治区之后的城市名（如"广东省广州萝岗"缺"市"）
    if any(
        re.search(rf"(?:省|自治区)[^省市区县]{{0,4}}{re.escape(k)}", arcade.location, re.I)
        for k in keys
    ):
        return True
    fields = [arcade.name, arcade.province]
    return any(k in f.lower() for k in keys for f in fields)


class ArcadeList(list[Arcade]):


    async def save_arcade(self):
        data = [_.model_dump() for _ in self]
        # 同步内存快照，否则凌晨4点下载机厅数据时会用启动时的旧快照覆盖，
        # 把运行期间设置的别名/订阅/人数全部清掉
        arcade.arcades = data
        return await writefile(arcades_json, data)
    
    def search_name(self, name: str, city: str = "") -> list[Arcade]:
        """模糊查询机厅，`city` 非空时仅查询该城市"""
        arcade_list = []
        name = name.lower()
        for arcade in self:
            if not match_city(arcade, city):
                continue
            if name in arcade.name.lower():
                arcade_list.append(arcade)
            elif name in arcade.location.lower():
                arcade_list.append(arcade)
            elif any(name in a.lower() for a in arcade.alias):
                arcade_list.append(arcade)

        return arcade_list
    
    def search_fullname(self, name: str, city: str = "") -> list[Arcade]:
        """查询店铺全名机厅，`city` 非空时仅查询该城市"""
        arcade_list = []
        for arcade in self:
            if not match_city(arcade, city):
                continue
            if name == arcade.name:
                arcade_list.append(arcade)

        return arcade_list
    
    def search_alias(self, alias: str, city: str = "") -> list[Arcade]:
        """查询别名机厅，`city` 非空时仅查询该城市"""
        arcade_list = []
        for arcade in self:
            if not match_city(arcade, city):
                continue
            if alias in arcade.alias:
                arcade_list.append(arcade)
        
        return arcade_list
    
    def search_id(self, id: str, city: str = "") -> list[Arcade]:
        """指定ID查询机厅，`city` 非空时仅查询该城市"""
        arcade_list = []
        for arcade in self:
            if not match_city(arcade, city):
                continue
            if id == arcade.id:
                arcade_list.append(arcade)

        return arcade_list

    def search_city(self, city: str) -> list[Arcade]:
        """查询指定城市的全部机厅"""
        return [arcade for arcade in self if match_city(arcade, city)]

    def add_arcade(self, arcade: dict) -> bool:
        """添加机厅"""
        self.append(Arcade.model_validate(arcade))
        return True

    def del_arcade(self, arcadeName: str) -> bool:
        """删除机厅"""
        for arcade in self:
            if arcadeName == arcade.name:
                self.remove(arcade)
                return True
        return False
    
    def group_in_arcade(self, group_id: int, arcadeName: str) -> bool:
        """是否已订阅该机厅"""
        for arcade in self:
            if arcadeName == arcade.name:
                if group_id in arcade.group:
                    return True
        return False
    
    def group_subscribe_arcade(self, group_id: int) -> list[Arcade]:
        """已订阅机厅"""
        arcade_list = []
        for arcade in self:
            if group_id in arcade.group:
                arcade_list.append(arcade)
        return arcade_list

    @classmethod
    def arcade_to_msg(cls, arcade_list: list[Arcade]) -> list[str]:
        """机厅人数格式化"""
        result = []
        for arcade in arcade_list:
            msg = f"""{arcade.name}
    - 当前 {arcade.person} 人\n"""
            if arcade.num > 1:
                msg += f"    - 平均 {arcade.person / arcade.num:.2f} 人\n"
            if arcade.by:
                msg += f"    - 由 {arcade.by} 更新于 {arcade.time}"
            result.append(msg.strip())
        return result


class ArcadeData:
    
    total: ArcadeList | None
    
    def __init__(self) -> None: 
        self.arcades = []
        if arcades_json.exists():
            self.arcades: list[dict] = json.load(open(arcades_json, "r", encoding="utf-8"))
        self.idList = []

    def get_by_id(self, id: int) -> dict:
        id_list = [c_a["id"] for c_a in self.arcades]
        if id in id_list:
            return self.arcades[id_list.index(id)]
        else:
            return None
    
    def fix_duplicate_ids(self) -> bool:
        """修复重复的机厅 id（旧版连续添加自建机厅会生成相同 id），返回是否有改动"""
        used: set[int] = {int(a.id) for a in self.total}
        seen: set[str] = set()
        next_id = max(10000, max(used) + 1 if used else 10000)
        changed = False
        for a in self.total:
            if a.id in seen:
                while next_id in used:
                    next_id += 1
                log.warning(f"机厅「{a.name}」的 id 重复（{a.id}），已重新分配为 {next_id}")
                a.id = str(next_id)
                used.add(next_id)
                next_id += 1
                changed = True
            seen.add(a.id)
        if changed:
            self.idList = [int(a.id) for a in self.total]
        return changed

    async def get_arcade(self):
        self.total = await download_arcade_info()
        self.idList = [int(c_a.id) for c_a in self.total]
        if self.fix_duplicate_ids():
            log.warning("检测到重复机厅 id，已自动修复并保存")
            await self.total.save_arcade()

arcade = ArcadeData()


class GroupCity:
    """群绑定的机厅城市，`{群号: 城市}`，未绑定的城市为空字符串"""

    def __init__(self) -> None:
        self.data: dict[str, str] = {}
        if arcade_city_json.exists():
            try:
                raw = json.load(open(arcade_city_json, "r", encoding="utf-8"))
                self.data = {str(k): str(v) for k, v in raw.items()}
            except Exception:
                log.error(f"读取群机厅城市绑定失败，已忽略\n{traceback.format_exc()}")
                self.data = {}

    def get(self, group_id: int) -> str:
        return self.data.get(str(group_id), "")

    async def set(self, group_id: int, city: str) -> None:
        self.data[str(group_id)] = city
        await writefile(arcade_city_json, self.data)

    async def remove(self, group_id: int) -> None:
        if str(group_id) in self.data:
            del self.data[str(group_id)]
            await writefile(arcade_city_json, self.data)


group_city = GroupCity()


def _official_arcade_to_dict(raw: dict, cached: dict | None = None) -> dict:
    arcade_dict = dict(cached or {})
    arcade_dict.update({
        "name": raw["arcadeName"],
        "location": raw["address"],
        "province": raw["province"],
        "mall": raw["mall"],
        "num": raw["machineCount"],
        "id": str(raw["id"]),
    })
    arcade_dict.setdefault("alias", [])
    arcade_dict.setdefault("group", [])
    arcade_dict.setdefault("person", 0)
    arcade_dict.setdefault("by", "")
    arcade_dict.setdefault("time", "")
    return arcade_dict


def _cached_arcades() -> ArcadeList:
    return ArcadeList(Arcade.model_validate(_) for _ in arcade.arcades)


async def download_arcade_info(save: bool = True) -> ArcadeList:
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(timeout=30)) as client:
            resp = await client.get("https://wc.wahlap.net/maidx/rest/location")
            resp.raise_for_status()
            data = resp.json()

        # 缓存优先取内存中最新的 total（含运行期设置的别名/订阅/人数），
        # total 为空时才退回启动时读盘的快照，防止旧快照覆盖丢失用户数据
        cached_list = (
            [_.model_dump() for _ in arcade.total]
            if getattr(arcade, "total", None)
            else arcade.arcades
        )
        cached_map = {str(_["id"]): _ for _ in cached_list}
        merged = ArcadeList()

        for raw in data:
            arcade_id = str(raw["id"])
            merged.append(
                Arcade.model_validate(
                    _official_arcade_to_dict(raw, cached_map.get(arcade_id))
                )
            )

        custom_arcades = [
            Arcade.model_validate(_)
            for _ in cached_list
            if int(_["id"]) >= 10000
        ]
        merged.extend(custom_arcades)

        arcade.arcades = [_.model_dump() for _ in merged]
        arcade.total = merged
        arcade.idList = [int(_.id) for _ in merged]
        if save:
            await writefile(arcades_json, arcade.arcades)
        return merged
    except Exception:
        log.error(f"Error: {traceback.format_exc()}")
        log.error("获取机厅信息失败，使用本地缓存")
        return _cached_arcades()


async def updata_arcade(arcadeName: str, num: str):
    if arcadeName.isdigit():
        arcade_list = arcade.total.search_id(arcadeName)
    else:
        arcade_list = arcade.total.search_fullname(arcadeName)
    if arcade_list:
        _arcade = arcade_list[0]
        _arcade.num = int(num)
        msg = f"已修改机厅 [{arcadeName}] 机台数量为 [{num}]"
        await arcade.total.save_arcade()
    else:
        msg = f"未找到机厅：{arcadeName}"
    return msg
    

async def update_alias(arcadeName: str, aliasName: str, add_del: bool):
    """变更机厅别名"""
    change = False
    if arcadeName.isdigit():
        arcade_list = arcade.total.search_id(arcadeName)
    else:
        arcade_list = arcade.total.search_fullname(arcadeName)
    if arcade_list:
        _arcade = arcade_list[0]
        if add_del:
            if aliasName not in _arcade.alias:
                _arcade.alias.append(aliasName)
                msg = f"机厅：{_arcade.name}\n已添加别名：{aliasName}"
                change = True
            else:
                msg = f"机厅：{_arcade.name}\n已拥有别名：{aliasName}\n请勿重复添加"
        else:
            if aliasName in _arcade.alias:
                _arcade.alias.remove(aliasName)
                msg = f"机厅：{_arcade.name}\n已删除别名：{aliasName}"
                change = True
            else:
                msg = f"机厅：{_arcade.name}\n未拥有别名：{aliasName}"
    else:
        msg = f"未找到机厅：{arcadeName}"
    if change:
        await arcade.total.save_arcade()
    return msg


async def subscribe(group_id: int, arcadeName: str, sub: bool, city: str = ""):
    """订阅机厅，`sub` 等于 `True` 为订阅，`False` 为取消订阅，`city` 非空时限定该城市"""
    change = False
    if arcadeName.isdigit():
        arcade_list = arcade.total.search_id(arcadeName, city)
    else:
        arcade_list = arcade.total.search_fullname(arcadeName, city)
    if arcade_list:
        _arcade = arcade_list[0]
        if sub:
            if arcade.total.group_in_arcade(group_id, _arcade.name):
                msg = f"该群已订阅机厅：{_arcade.name}"
            else:
                _arcade.group.append(group_id)
                msg = f"群：{group_id} 已添加订阅机厅：{_arcade.name}"
                change = True
        else:
            if not arcade.total.group_in_arcade(group_id, _arcade.name):
                msg = f"该群未订阅机厅：{_arcade.name}，无需取消订阅"
            else:
                _arcade.group.remove(group_id)
                msg = f"群：{group_id} 已取消订阅机厅：{_arcade.name}"
                change = True
    else:
        msg = f"未找到机厅：{arcadeName}"
    if change:
        await arcade.total.save_arcade() 
    return msg
        
        
async def update_person(arcadeList: list[Arcade], userName: str, value: str, person: int):
    """变更机厅人数"""
    if len(arcadeList) == 1:
        _arcade = arcadeList[0]
        original_person = _arcade.person
        if value in ["+", "＋", "增加", "添加", "加"]:
            if person > 30:
                return "请勿乱玩bot，恼！"
            _arcade.person += person
        elif value in ["-", "－", "减少", "降低", "减"]:
            if person > 30 or person > _arcade.person:
                return "请勿乱玩bot，恼！"
            _arcade.person -= person
        elif value in ["=", "＝", "设置", "设定"]:
            if abs(_arcade.person - person) > 30:
                return "请勿乱玩bot，恼！"
            _arcade.person = person
        if _arcade.person == original_person:
            return f"人数没有变化\n机厅：{_arcade.name}\n当前人数：{_arcade.person}"
        else:
            _arcade.by = userName
            _arcade.time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
            await arcade.total.save_arcade()
            return f"机厅：{_arcade.name}\n当前人数：{_arcade.person}\n变更时间：{_arcade.time}"
    elif len(arcadeList) > 1:
        return "找到多个机厅，请使用id变更人数\n" + "\n".join([f"{_.id}：{_.name}" for _ in arcadeList])
    else:
        return "没有找到指定机厅"
