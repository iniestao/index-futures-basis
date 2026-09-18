# -*- coding: utf-8 -*-
"""每日增量更新编排：抓取最新数据 -> 重算 -> 输出（GitHub Actions 每日调用）"""
import os, sys, subprocess, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import env_setup, PROJ, OUT
env_setup()

SCRIPTS = os.path.dirname(os.path.abspath(__file__))
# 逐步耗时落盘（随提交入库）：Actions 日志对外不可读，而"哪一步慢"只能靠它回答。
# 外部接口（东财/新浪）夜间限流强度逐日波动，同类运行耗时可在 1h~2.5h 间摆动，
# 没有这份记录就只能靠猜。
TIMINGS = os.path.join(OUT, "step_timings.csv")

def run(script, *args):
    cmd = [sys.executable, os.path.join(SCRIPTS, script), *args]
    print(f"▶ {script} {' '.join(args)}", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=SCRIPTS)
    dt = time.time() - t0
    print(f"  done in {dt:.0f}s (exit {r.returncode})", flush=True)
    try:
        exists = os.path.exists(TIMINGS)
        with open(TIMINGS, "a", encoding="utf-8") as f:
            if not exists:
                f.write("run_at,script,seconds,exit\n")
            f.write(f"{time.strftime('%Y-%m-%d %H:%M')},{script},{dt:.0f},{r.returncode}\n")
    except Exception as e:
        print(f"  [WARN] 耗时记录失败: {type(e).__name__}: {e}", flush=True)
    return r.returncode == 0

def main():
    ok = True
    # 1. 当前月末权重快照（兜底用；历史月度权重为静态文件不更新）
    ok &= run("fetch_weights.py")
    # 2. 指数日线（增量覆盖）
    ok &= run("fetch_index_prices.py")
    # 3. 中金所月度包：默认只补当前月与上一月
    ok &= run("fetch_cffex_monthly.py")
    # 4. 分红明细：内容驱动轮转刷新（活跃股每2天一刷，沉睡股每10天一刷，不依赖 mtime）
    ok &= run("fetch_dividends.py")
    # 5. 年报 EPS：最近两年报告期
    ok &= run("fetch_eps_annual.py")
    # 5b. 季报 EPS（累计值 + 披露日）：pq 口径所需，历史只补缺失、最近2期刷新
    ok &= run("fetch_eps_quarterly.py")
    # 5c. 个股日线（不复权收盘价）：每日权重的流通市值漂移所需。
    #     首次运行为全量回补（universe 缺口），之后每日只增量刷新当前成分
    ok &= run("fetch_stock_prices.py")
    # 6. 计算
    ok &= run("compute_adjusted_basis.py")
    ok &= run("compute_backtest.py")
    ok &= run("make_outputs.py")
    # 7. 数据质量护栏（隐含股息率/覆盖率，告警不阻断）
    run("check_data_quality.py")
    print("ALL DONE" if ok else "PARTIAL FAILURE", flush=True)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
