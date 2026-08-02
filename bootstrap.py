#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""一鍵安裝器：建虛擬環境 → 裝套件 → 抓 ffmpeg → 自檢。

**這支檔案只准用標準函式庫**，因為它是在「什麼都還沒裝」的系統 Python 上跑的第一支程式。
任何 import 第三方套件的行為都會讓乾淨機器直接失敗。

用法：
    python bootstrap.py              # 全部做完
    python bootstrap.py --no-deps    # 只抓 ffmpeg，不裝 Python 套件
    python bootstrap.py --force      # 重建 venv、重抓 ffmpeg
"""

import argparse
import subprocess
import sys
import venv
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bearcut.env import ffmpeg as _ffmpeg
from bearcut.env.platform import (MIN_PYTHON, ROOT, VENV, console_utf8,
                                  python_ok, python_version, venv_python)

console_utf8()

REQUIREMENTS = ROOT / "requirements.txt"


def say(msg: str = "") -> None:
    print(msg, flush=True)


def step(n: int, total: int, msg: str) -> None:
    say(f"\n[{n}/{total}] {msg}")


def fail(msg: str) -> int:
    say("\n" + "─" * 56)
    say("  安裝沒有完成")
    say("─" * 56)
    say(msg)
    say("")
    return 1


def ensure_python() -> bool:
    if python_ok():
        say(f"  ✓ Python {python_version()}")
        return True
    say(f"  ✗ Python {python_version()}，需要 "
        f"{'.'.join(map(str, MIN_PYTHON))} 以上")
    say("    請到 https://www.python.org/downloads/ 下載新版後重跑本程式。")
    return False


def ensure_venv(force: bool) -> bool:
    """建立 .venv。已存在就沿用（除非 --force）。"""
    py = venv_python()
    if py.exists() and not force:
        say(f"  ✓ 已存在：{VENV.name}")
        return True
    if force and VENV.exists():
        import shutil
        say("  · 移除舊的虛擬環境…")
        shutil.rmtree(VENV, ignore_errors=True)
    say("  · 建立虛擬環境（約 10 秒）…")
    try:
        venv.EnvBuilder(with_pip=True, clear=False).create(VENV)
    except Exception as e:
        say(f"  ✗ 建立失敗：{e}")
        return False
    ok = venv_python().exists()
    say("  ✓ 完成" if ok else "  ✗ 建好了但找不到直譯器")
    return ok


def install_deps() -> bool:
    """把 requirements.txt 裝進 venv。"""
    if not REQUIREMENTS.exists():
        say(f"  ! 找不到 {REQUIREMENTS.name}，略過")
        return True
    py = str(venv_python())
    say("  · 更新 pip…")
    subprocess.run([py, "-m", "pip", "install", "-q", "--upgrade", "pip"],
                   check=False)
    say("  · 安裝相依套件（第一次會下載較久，請耐心等）…")
    r = subprocess.run([py, "-m", "pip", "install", "-r", str(REQUIREMENTS)])
    if r.returncode != 0:
        say("  ✗ 套件安裝失敗（上面有 pip 的訊息）")
        return False
    say("  ✓ 完成")
    return True


def install_ffmpeg(force: bool) -> bool:
    res = _ffmpeg.install(progress_cb=lambda p, m: say(f"    [{int(p):3d}%] {m}"),
                          force=force)
    if res["ok"]:
        say(f"  ✓ {res['ffmpeg']}")
        return True
    say("  ✗ " + (res.get("error") or "未知錯誤"))
    return False


def run_doctor() -> bool:
    """用 venv 的直譯器跑自檢——套件裝在那裡，用系統 Python 檢查會誤報。"""
    py = venv_python()
    exe = str(py) if py.exists() else sys.executable
    r = subprocess.run([exe, str(ROOT / "cli.py"), "doctor"])
    return r.returncode == 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="BearCut 一鍵安裝")
    ap.add_argument("--no-deps", action="store_true", help="不裝 Python 套件")
    ap.add_argument("--force", action="store_true", help="重建虛擬環境、重抓 ffmpeg")
    args = ap.parse_args(argv)

    say("")
    say("  BearCut 安裝程式")
    say("  © 熊董 × 川輝科技 Brightstream Technology")
    say("  " + "─" * 52)
    say("  會在這個資料夾底下裝好所有東西，不會動到你的系統設定。")

    total = 2 if args.no_deps else 5   # python, [venv, deps, gpu,] ffmpeg
    n = 0

    n += 1
    step(n, total, "檢查 Python")
    if not ensure_python():
        return fail("請先安裝新版 Python，再重新執行。")

    if not args.no_deps:
        n += 1
        step(n, total, "建立虛擬環境")
        if not ensure_venv(args.force):
            return fail("無法建立虛擬環境。請確認這個資料夾有寫入權限，\n"
                        "或把整個資料夾移到桌面等一般位置後重試。")

        n += 1
        step(n, total, "安裝 Python 套件")
        if not install_deps():
            return fail("套件安裝失敗。常見原因與解法：\n"
                        "  · 網路不穩 → 重跑一次通常就好\n"
                        "  · 公司網路擋 PyPI → 換網路或設定 pip 鏡像")

    if not args.no_deps:
        n += 1
        step(n, total, "GPU 加速（選用）")
        from bearcut.env import cuda as _cuda
        res = _cuda.install(progress_cb=lambda p_, m: say(f"    {m}"))
        say(f"  {'✓' if res['ok'] else '·'} {res['reason']}")

    n += 1
    step(n, total, "取得 FFmpeg")
    if not install_ffmpeg(args.force):
        return fail("FFmpeg 沒裝成功，但其他部分沒問題。\n"
                    "照上面的指示手動安裝後，重跑 python bootstrap.py 即可。")

    say("\n" + "─" * 56)
    ok = run_doctor()
    if ok:
        say("  安裝完成，可以開始用了。")
        say("  下一步：雙擊 START_HERE，或執行  python cli.py --help")
    say("")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
