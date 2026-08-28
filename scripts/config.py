# -*- coding: utf-8 -*-
"""公共配置：股指期货剔除分红基差流水线（纯公开数据）"""
import os

PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW = os.path.join(PROJ, "data_raw")
OUT = os.path.join(PROJ, "output")

START = "2015-01-01"
END = "2026-12-31"

# 品种 -> 指数代码 / 新浪指数代码 / 首个合约月
PRODUCTS = {
    "IF": dict(index="000300", sina_idx="sh000300", first_month=201501),
    "IH": dict(index="000016", sina_idx="sh000016", first_month=201501),
    "IC": dict(index="000905", sina_idx="sh000905", first_month=201501),
    "IM": dict(index="000852", sina_idx="sh000852", first_month=202207),  # IM 2022-07 上市
}

def env_setup():
    """数据源全部为公开站点；如本地网络需要代理，请自行 export HTTPS_PROXY/HTTP_PROXY。"""
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")

def month_range(first_month, last_month):
    y, m = divmod(first_month, 100)
    out = []
    while y * 100 + m <= last_month:
        out.append(y * 100 + m)
        m += 1
        if m == 13:
            y, m = y + 1, 1
    return out

def third_friday(ym):
    """YYYYMM 月份第三个周五（中金所股指期货交割日）"""
    import datetime
    y, m = divmod(ym, 100)
    d = datetime.date(y, m, 1)
    fridays = []
    while d.month == m:
        if d.weekday() == 4:
            fridays.append(d)
        d += datetime.timedelta(days=1)
    return fridays[2]
