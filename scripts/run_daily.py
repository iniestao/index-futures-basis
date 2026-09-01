# -*- coding: utf-8 -*-
"""每日增量更新编排：抓取最新数据 -> 重算 -> 输出（GitHub Actions 每日调用）"""
import os, sys, subprocess, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from config import env_setup, PROJ
env_setup()

SCRIPTS = os.path.dirname(os.path.abspath(__file__))

def run(script, *args):
    cmd = [sys.executable, os.path.join(SCRIPTS, script), *args]
    print(f"▶ {script} {' '.join(args)}", flush=True)
    t0 = time.time()
    r = subprocess.run(cmd, cwd=SCRIPTS)
    print(f"  done in {time.time()-t0:.0f}s (exit {r.returncode})", flush=True)
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
    # 6. 计算
    ok &= run("compute_adjusted_basis.py")
    ok &= run("compute_backtest.py")
    ok &= run("make_outputs.py")
    print("ALL DONE" if ok else "PARTIAL FAILURE", flush=True)
    sys.exit(0 if ok else 1)

if __name__ == "__main__":
    main()
