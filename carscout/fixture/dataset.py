"""Deterministic multi-platform fixture dataset.

Four platforms with deliberately different transport mechanics:
  A  static HTML list + paginated detail pages (10 per page)
  B  rendered page + scroll-triggered XHR list, detail returned as XHR JSON
  C  static HTML behind an anti-bot wall (200 + empty payload when throttled)
  D  App-style signed JSON API (HMAC signature, short-lived window), invoked
     from an in-page browser session
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

TODAY = date(2026, 11, 12)


@dataclass(frozen=True)
class Car:
    cid: str
    brand: str
    series: str
    model: str
    reg_year: int
    reg_month: int
    mileage: int
    displacement: int          # ml; 0 => EV
    transmission: str
    transfer: int
    city: str
    seller: str
    vin: str | None = None
    fuel: str = "汽油"
    color: str = "黑色"


CARS: dict[str, Car] = {
    "v1": Car("v1", "宝马", "3系", "2018款 320Li M运动套装", 2018, 6, 60000, 1998, "8挡手自一体", 1, "南京", "个人",
              vin="LFV3A28G8J1234ABC", fuel="汽油", color="白色"),
    "v2": Car("v2", "宝马", "3系", "2019款 320Li 时尚型", 2019, 3, 41000, 1998, "8挡手自一体", 0, "南京", "个人",
              vin="LBV3B38G9K7654XYZ", fuel="汽油", color="蓝色"),
    "v3": Car("v3", "奥迪", "A4L", "2018款 40 TFSI 进取型", 2018, 9, 72000, 1984, "7挡双离合", 2, "苏州", "个人",
              vin="LFV3C28G0J000A12", fuel="汽油", color="灰色"),
    "v4": Car("v4", "大众", "迈腾", "2017款 330TSI DSG 领先型", 2017, 5, 110000, 1798, "7挡双离合", 1, "南京", "商家"),
    "v5": Car("v5", "丰田", "凯美瑞", "2019款 2.5L 豪华版", 2019, 11, 38000, 2487, "无级变速", 0, "苏州", "个人",
              vin="LVGTF1EK2K0025CAM", fuel="汽油", color="银色"),
    "v6": Car("v6", "本田", "雅阁", "2018款 260TURBO 精英版", 2018, 4, 85000, 1498, "无级变速", 1, "南京", "个人",
              vin="LHGCV1620J80260AC", fuel="汽油", color="黑色"),
    "v7": Car("v7", "奔驰", "C级", "2020款 C260L 运动版", 2020, 1, 29000, 1497, "9挡手自一体", 0, "苏州", "商家",
              vin="LBVCU4GB2L000BEN", fuel="汽油", color="白色"),
    "v8": Car("v8", "特斯拉", "Model 3", "2021款 标准续航后驱升级版", 2021, 7, 22000, 0, "固定齿比", 0, "南京", "个人",
              fuel="纯电动", color="白色"),
    "v9": Car("v9", "比亚迪", "汉", "2022款 EV 尊贵型", 2022, 3, 18000, 0, "固定齿比", 0, "苏州", "个人",
              vin="LGXCE4CB8N000BYD", fuel="纯电动", color="黑色"),
    "v10": Car("v10", "大众", "帕萨特", "2016款 280TSI DSG 尊雅版", 2016, 8, 130000, 1395, "7挡双离合", 2, "无锡", "商家",
               fuel="汽油", color="黑色"),
    "v11": Car("v11", "奥迪", "Q5L", "2019款 40 TFSI 荣享进取型", 2019, 6, 52000, 1984, "7挡双离合", 0, "南京", "个人",
               vin="LFV3B28R2K340Q5L", fuel="汽油", color="黑色"),
    "v12": Car("v12", "丰田", "汉兰达", "2018款 2.0T 四驱精英版", 2018, 12, 90000, 1998, "6挡手自一体", 1, "无锡", "个人",
               fuel="汽油", color="白色"),
}

PLATFORM_META = {
    "A": {"name": "平台A · 静态列表", "page_size": 6},
    "B": {"name": "平台B · 滚动加载", "page_size": 8},
    "C": {"name": "平台C · 风控平台", "page_size": 10},
    "D": {"name": "平台D · App签名接口", "page_size": 10},
}


@dataclass
class Listing:
    platform: str
    pid: str
    car: Car
    price_history: list[tuple[int, int]]      # (days_ago, price); newest first is NOT required
    listed_days_ago: int
    removed_days_ago: int | None = None
    vin_mode: str | None = None                # full | tail6 | None
    image_mode: str = "same"                   # same | alt | none
    mileage: int | None = None
    reg_year: int | None = None
    reg_month: int | None = None
    city: str | None = None
    transfer: int | None = None
    seller: str | None = None
    desc_style: str = "standard"               # standard | terse | verbose | alt
    extra_desc: str = ""

    @property
    def eff_mileage(self) -> int:
        return self.car.mileage if self.mileage is None else self.mileage

    @property
    def eff_reg(self) -> tuple[int, int]:
        return (self.car.reg_year, self.car.reg_month) if self.reg_year is None else (self.reg_year, self.reg_month)

    @property
    def eff_city(self) -> str:
        return self.car.city if self.city is None else self.city

    @property
    def eff_transfer(self) -> int:
        return self.car.transfer if self.transfer is None else self.transfer

    @property
    def eff_seller(self) -> str:
        return self.car.seller if self.seller is None else self.seller

    @property
    def listed_at(self) -> datetime:
        return datetime.combine(TODAY - timedelta(days=self.listed_days_ago), time(10, 0), tzinfo=timezone.utc)

    @property
    def removed_at(self) -> datetime | None:
        if self.removed_days_ago is None:
            return None
        return datetime.combine(TODAY - timedelta(days=self.removed_days_ago), time(10, 0), tzinfo=timezone.utc)

    def price_on(self, day: date) -> int | None:
        """Latest price at or before `day`, or None if the listing was not visible then."""
        if self.listed_at.date() > day:
            return None
        if self.removed_at is not None and self.removed_at.date() <= day:
            return None
        available = [(TODAY - timedelta(days=off), p) for off, p in self.price_history if off >= 0]
        best = None
        for d, p in available:
            if d <= day:
                if best is None or d >= best[0]:
                    best = (d, p)
        if best is None:
            # before first recorded history point but after listing: use oldest point
            oldest = min(available, key=lambda x: x[0])
            return oldest[1]
        return best[1]

    def current_price(self, day: date = TODAY) -> int | None:
        return self.price_on(day)

    @property
    def vin_value(self) -> str | None:
        if self.vin_mode == "full":
            return self.car.vin
        if self.vin_mode == "tail6" and self.car.vin:
            return "******" + self.car.vin[-6:]
        return None


LISTINGS: list[Listing] = [
    # V1 BMW 3系 2018 — listed on all four platforms, VIN exposed on A/B
    Listing("A", "pa-1001", CARS["v1"], [(30, 199000), (14, 192000), (3, 188000)], 45, vin_mode="full"),
    Listing("B", "pb-2001", CARS["v1"], [(28, 189000), (8, 185000)], 40, vin_mode="tail6", desc_style="terse"),
    Listing("C", "pc-3001", CARS["v1"], [(25, 191000), (5, 189000)], 32, desc_style="verbose", extra_desc="急售，诚心要可小刀"),
    Listing("D", "pd-4001", CARS["v1"], [(30, 190000), (5, 186000)], 44, desc_style="terse"),

    # V2 BMW 3系 2019 — same car on A/B/D, non-VIN auto match
    Listing("A", "pa-1002", CARS["v2"], [(15, 173000)], 22),
    Listing("B", "pb-2002", CARS["v2"], [(12, 171000)], 20, vin_mode="tail6", desc_style="terse"),
    Listing("D", "pd-4002", CARS["v2"], [(15, 174000)], 23, vin_mode="full", desc_style="terse"),

    # V3 Audi A4L — VIN tail matches A & C directly; D merges by features
    Listing("A", "pa-1003", CARS["v3"], [(50, 165000), (21, 161000), (6, 158000)], 50, vin_mode="tail6"),
    Listing("C", "pc-3003", CARS["v3"], [(38, 162000)], 38, vin_mode="tail6", extra_desc="可分期"),
    Listing("D", "pd-4003", CARS["v3"], [(41, 159000)], 41, desc_style="terse"),

    # V4 VW Magotan
    Listing("B", "pb-2004", CARS["v4"], [(60, 105000)], 60, desc_style="verbose"),
    Listing("C", "pc-3004", CARS["v4"], [(55, 102000)], 55),

    # V5 Toyota Camry
    Listing("A", "pa-1005", CARS["v5"], [(40, 138000)], 40, vin_mode="tail6"),
    Listing("B", "pb-2005", CARS["v5"], [(37, 140000)], 36, desc_style="terse"),
    Listing("D", "pd-4005", CARS["v5"], [(33, 139000)], 33, desc_style="terse"),

    # V6 Honda Accord
    Listing("A", "pa-1006", CARS["v6"], [(47, 98000)], 47),
    Listing("C", "pc-3006", CARS["v6"], [(44, 99000)], 44, desc_style="verbose"),

    # V7 Mercedes C
    Listing("B", "pb-2007", CARS["v7"], [(29, 260000)], 29, vin_mode="tail6", desc_style="terse"),
    Listing("D", "pd-4007", CARS["v7"], [(26, 258000)], 26, desc_style="terse"),

    # V8 Tesla Model 3
    Listing("C", "pc-3008", CARS["v8"], [(18, 185000)], 18),
    Listing("D", "pd-4008", CARS["v8"], [(17, 183000)], 17, desc_style="terse"),

    # V9 BYD Han — four-platform cluster
    Listing("A", "pa-1009", CARS["v9"], [(35, 172000)], 35, vin_mode="tail6"),
    Listing("B", "pb-2009", CARS["v9"], [(31, 170000)], 30, desc_style="terse"),
    Listing("C", "pc-3009", CARS["v9"], [(28, 169000)], 28, desc_style="verbose"),
    Listing("D", "pd-4009", CARS["v9"], [(26, 171000)], 26, desc_style="terse"),

    # V10 VW Passat
    Listing("A", "pa-1010", CARS["v10"], [(58, 62000)], 58),
    Listing("C", "pc-3010", CARS["v10"], [(52, 61000)], 52),

    # V11 Audi Q5L
    Listing("B", "pb-2011", CARS["v11"], [(24, 215000)], 24, vin_mode="tail6", desc_style="terse"),
    Listing("C", "pc-3011", CARS["v11"], [(22, 212000)], 22),
    Listing("D", "pd-4011", CARS["v11"], [(21, 213000)], 21, desc_style="terse"),

    # V12 Toyota Highlander
    Listing("A", "pa-1012", CARS["v12"], [(70, 168000)], 70),
    Listing("B", "pb-2012", CARS["v12"], [(64, 167000)], 63, desc_style="verbose"),

    # ------------------------------------------------------------------
    # Intentionally generated grey-zone pairs (must NOT auto-merge)
    # ------------------------------------------------------------------
    # G1: Audi A4L on B, odometer mis-keyed, different photos & wording
    Listing("B", "pb-2013", CARS["v3"], [(19, 155000)], 19, mileage=78500, transfer=1,
            image_mode="alt", desc_style="alt"),
    # G2: Camry on C, 44k on the clock, advertised from a neighbouring city,
    #     second photo set and a differently written description
    Listing("C", "pc-3013", CARS["v5"], [(16, 135000)], 16, mileage=44000, transfer=1,
            city="常州", image_mode="alt", desc_style="alt"),
    # G3: Mercedes C on A, registration month off by two, different photos
    Listing("A", "pa-1014", CARS["v7"], [(13, 255000)], 13, reg_year=2019, reg_month=11,
            image_mode="alt", desc_style="alt"),

    # Sold/removed listing to exercise the 30-day window and disappearance
    Listing("A", "pa-1015", CARS["v6"], [(45, 95000)], 49, removed_days_ago=9),
]

LISTINGS_BY_PLATFORM: dict[str, list[Listing]] = {
    code: [l for l in LISTINGS if l.platform == code] for code in PLATFORM_META
}

# ---------------------------------------------------------------------------
# descriptions
# ---------------------------------------------------------------------------
SHARED_PHRASES = {
    "v1": ["个人一手车，保养记录齐全", "四轮原版，无重大事故", "内饰保养好，无烟味"],
    "v2": ["家庭代步用车，平时上下班开", "4S店保养，记录可查", "车况精神"],
    "v3": ["内饰整洁，按时保养", "个人车源，手续齐全", "原厂漆面保持良好"],
    "v4": ["商务用车，空间宽敞", "发动机变速箱状态良好", "可提供保养记录"],
    "v5": ["一手车源，省油耐用", "无事故无泡水", "四条轮胎几乎全新"],
    "v6": ["代步好选择，动力平顺", "全车原版，钣金无修复", "支持第三方检测"],
    "v7": ["豪华品牌，配置齐全", "车况精品，随时看车", "4S店全程保养"],
    "v8": ["电池健康度良好", "家充为主，无事故", "自动驾驶功能正常"],
    "v9": ["准新车况，原版原漆", "电池无衰减", "支持任意检测"],
    "v10": ["空间大，家用商务两相宜", "保养及时，发动机安静", "价格实在"],
    "v11": ["四驱系统状态正常", "全程4S保养", "内饰几乎无磨损"],
    "v12": ["七座大空间，家庭出行", "越野能力好", "车况稳定"],
}
ALT_DESCRIPTIONS = {
    "v3": "车辆为外地转入，公里数以实际为准。接受任意检测，价格面议，联系人王先生。",
    "v5": "车主置换出售，车在店里。外观有轻微划痕，配置以实车为准。",
    "v7": "单位淘汰车辆，手续资料齐全。看车需提前预约，不接受远程议价。",
}
FORMAL_NOISE = ["支持第三方检测", "手续齐全可随时过户", "看车方便", "价格可谈"]
TERSE_NOISE = ["实拍", "随时看车", "非诚勿扰"]


def _mileage_wan(km: int) -> str:
    return f"{km / 10000:.1f}万公里"


def title_for(l: Listing, style: str | None = None) -> str:
    car = l.car
    reg_y, reg_m = l.eff_reg
    base = f"{car.brand}{car.series}"
    style = style or l.platform
    if style == "A":
        return f"【{l.eff_city}】{base} {car.model} {l.eff_mileage / 10000:.1f}万公里 上牌{reg_y}年{reg_m}月"
    if style == "B":
        return f"{base} {car.model.replace('款', '')} {_mileage_wan(l.eff_mileage)} {reg_y}/{reg_m:02d}上牌"
    if style == "C":
        return f"{base} {car.model}｜{reg_y}年{reg_m}月上牌｜{_mileage_wan(l.eff_mileage)}｜{l.eff_city}"
    return f"{base} {car.model} {reg_y}-{reg_m:02d} {l.eff_mileage}km"


def description_for(l: Listing) -> str:
    car = l.car
    shared = SHARED_PHRASES.get(car.cid, ["车况良好"])
    if l.desc_style == "alt":
        return ALT_DESCRIPTIONS.get(car.cid, "车况以实车为准，欢迎到店看车试驾。")
    reg_y, reg_m = l.eff_reg
    core = f"{reg_y}年{reg_m}月上牌，表显{l.eff_mileage}公里，{car.displacement or '纯电'}{'ml' if car.displacement else ''}，{car.transmission}，过户{l.eff_transfer}次，{l.eff_city}看车。"
    if l.desc_style == "terse":
        body = "；".join(shared[:1] + TERSE_NOISE[:2])
    elif l.desc_style == "verbose":
        body = "；".join(shared + FORMAL_NOISE[:2] + [l.extra_desc] if l.extra_desc else shared + FORMAL_NOISE[:2])
    else:
        body = "；".join(shared[:2] + FORMAL_NOISE[:1])
        if l.extra_desc:
            body += "；" + l.extra_desc
    return f"{core}{body}。"


# ---------------------------------------------------------------------------
# helpers used by the fixture HTTP layer
# ---------------------------------------------------------------------------
def all_platforms() -> list[str]:
    return list(PLATFORM_META)


def listings_on(platform: str, day: date) -> list[Listing]:
    out = []
    for l in LISTINGS_BY_PLATFORM[platform]:
        if l.price_on(day) is not None:
            out.append(l)
    return out


def listing_by_pid(platform: str, pid: str) -> Listing | None:
    for l in LISTINGS_BY_PLATFORM[platform]:
        if l.pid == pid:
            return l
    return None


def price_expectation(day: date) -> dict[str, dict]:
    """pid -> expected visible price for a snapshot date (used by tests/tools)."""
    res = {}
    for l in LISTINGS:
        p = l.price_on(day)
        if p is not None:
            res[l.pid] = {"price": p, "listed_at": l.listed_at.isoformat()}
    return res


def gray_zone_pids() -> list[str]:
    return ["pb-2013", "pc-3013", "pa-1014"]
