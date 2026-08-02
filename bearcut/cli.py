#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""BearCut 指令列入口。

給 AI Agent 的提示：每個子指令都支援 `--json`，會把結構化結果印到 stdout。
先跑 `python cli.py doctor --json`，看 `ok` 欄位決定要不要先跑 `python bootstrap.py`。
"""

import argparse
import json
import sys

from . import __version__
from .env import doctor as _doctor
from .env import ffmpeg as _ffmpeg
from .env.platform import console_utf8

console_utf8()


def _emit(data: dict, as_json: bool, human: str = "") -> None:
    if as_json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
    else:
        print(human)


def cmd_doctor(args) -> int:
    res = _doctor.check()
    _emit(res, args.json, _doctor.render(res))
    return 0 if res["ok"] else 1


def cmd_setup(args) -> int:
    """只補外部工具（ffmpeg）。要建 venv、裝套件請用 bootstrap.py。"""
    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    res = _ffmpeg.install(progress_cb=prog, force=args.force)
    human = ("\n  ffmpeg 就緒：" + res.get("ffmpeg", "")) if res["ok"] else \
            ("\n  失敗：\n" + (res.get("error") or ""))
    _emit(res, args.json, human)
    return 0 if res["ok"] else 1


def cmd_cut(args) -> int:
    """順剪一支影片。"""
    from .pipeline import analyze, cut_from_plan

    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    if args.apply:
        out = cut_from_plan(args.apply, output_dir=args.out, progress_cb=prog)
        _emit({"ok": True, "output": out}, args.json, f"\n淨毛片：{out}")
        return 0

    res = analyze(args.video, do_cut=not args.plan_only,
                  output_dir=args.out, profile=args.mode, progress_cb=prog)
    s = res["plan_data"]["summary"]
    human = (f"\n  原長 {s['原長秒']}s → 剪後 {s['剪後秒']}s（剪掉 {s['剪掉比例']}）\n"
             f"  待剪清單：{res['plan']}\n"
             f"  複核對照：{res['review']}\n"
             f"  字幕校對：{res['v1']}\n"
             f"  SRT　　：{res['srt']}"
             + (f"\n  淨毛片　：{res['output']}" if "output" in res else ""))
    _emit({"ok": True, **{k: v for k, v in res.items() if k != "plan_data"},
           "summary": s}, args.json, human)
    return 0


def cmd_shortform(args) -> int:
    """把剪好的片做成直式短影音。"""
    from .shortform import make

    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    res = make(args.video, srt=args.srt, title=args.title, cta=args.cta,
               output_dir=args.out, use_cards=not args.no_cards,
               follow_speaker=args.follow, make_cover=not args.no_cover,
               progress_cb=prog)
    human = (f"\n  直式短片：{res['video']}"
             + (f"\n  封面　　：{res['cover']}" if res.get("cover") else "")
             + f"\n  大字卡　：{len(res.get('cards') or [])} 張")
    _emit({"ok": True, **res}, args.json, human)
    return 0


def cmd_verify(args) -> int:
    """檢查一支已經剪好的成片。"""
    import os
    from . import srtlint, syncqa, visualqa

    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    video = args.video
    base = os.path.splitext(video)[0]
    srt = args.srt or (base + "_字幕.srt")
    srt = srt if os.path.exists(srt) else None

    issues = [f"[音畫字] {x}" for x in syncqa.check(video, srt=srt, progress_cb=prog)]
    if srt:
        issues += [f"[字幕] {x}" for x in srtlint.lint(srt)]

    frames = {}
    if args.frames:
        import json as _json
        plan_p = base.replace("_淨毛片", "") + "_待剪清單.json"
        keep = []
        if os.path.exists(plan_p):
            keep = _json.load(open(plan_p, encoding="utf-8")).get("keep") or []
        if keep:
            frames = visualqa.extract(video, keep, base + "_接縫畫面", progress_cb=prog)
        else:
            print("  找不到待剪清單，無法定位接縫，略過畫面抽幀")

    human = ("\n  ✓ 沒有發現問題\n" if not issues else
             f"\n  發現 {len(issues)} 項：\n" + "\n".join(f"    · {x}" for x in issues))
    _emit({"ok": not issues, "issues": issues, "frames": frames}, args.json, human)
    return 0


def cmd_update(args) -> int:
    """檢查、安裝或還原規則包。"""
    from . import update as _up

    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    if args.rollback:
        res = _up.rollback(progress_cb=prog)
        _emit(res, args.json,
              f"\n  {'已還原：' + res['restored'] if res['ok'] else res['error']}")
        return 0 if res["ok"] else 1

    info = _up.check(feed=args.feed, token=args.token)
    if info.get("error") and not info.get("available"):
        _emit(info, args.json, f"\n  {info['error']}")
        return 0
    if not info["available"]:
        _emit(info, args.json,
              f"\n  已是最新版（規則包 {info.get('current') or '未安裝'}）")
        return 0

    human = (f"\n  有新版規則包：{info['current'] or '未安裝'} → {info['latest']}\n"
             + (f"\n{info['notes'][:400]}\n" if info.get("notes") else ""))
    if args.check_only:
        _emit(info, args.json, human + "\n  用 bearcut update 安裝。")
        return 0

    print(human)
    res = _up.install_from(info["url"], expect_sha=info.get("sha256"),
                           token=args.token, progress_cb=prog)
    _emit({**info, **res}, args.json,
          f"\n  {'已更新到 ' + str(res['version']) if res['ok'] else res['error']}"
          + (f"\n  ⚠ {res['warning']}" if res.get("warning") else "")
          + ("\n  不滿意可以用 bearcut update --rollback 還原。" if res["ok"] else ""))
    return 0 if res["ok"] else 1


def cmd_longform(args) -> int:
    """長片：一個資料夾裡的多段影片，各自順剪再接成一支。"""
    from . import longform as _lf

    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    try:
        res = _lf.process_folder(args.folder, output_dir=args.out,
                                 output_name=args.name, profile=args.mode,
                                 force=args.force, progress_cb=prog)
    except (RuntimeError, NotADirectoryError) as e:
        _emit({"ok": False, "error": str(e)}, args.json, f"\n  {e}\n")
        return 1

    human = (f"\n  {len(res['parts'])} 段接成一支\n"
             f"  完整淨毛片：{res['output']}\n"
             + (f"  完整字幕：{res['srt']}\n" if res.get("srt") else
                "  （各段有自己的字幕，但合併時間軸沒成功）\n"))
    _emit({"ok": True, **res}, args.json, human)
    return 0


def cmd_highlights(args) -> int:
    """從長片自動挑出精華段落，各自做成直式短影音。"""
    from . import highlights as _hl
    from .rules import RulepackError

    def prog(p, m):
        if not args.json:
            print(f"  [{int(p):3d}%] {m}", flush=True)

    try:
        res = _hl.make(args.video, srt=args.srt, count=args.count,
                       output_dir=args.out, plan_only=args.plan_only,
                       progress_cb=prog)
    except (RulepackError, RuntimeError, FileNotFoundError) as e:
        _emit({"ok": False, "error": str(e)}, args.json, f"\n  {e}\n")
        return 1

    if not res["clips"]:
        _emit({"ok": True, **res}, args.json,
              "\n  沒有選出夠好的段落。這支片可能偏鋪陳，或字幕太短。\n")
        return 0

    lines = [f"\n  候選 {len(res['clips'])} 條，清單：{res['plan']}"]
    for i, c in enumerate(res["outputs"], 1):
        lines.append(f"\n  {i}. {c['title'] or c['text'][:24]}"
                     f"（{c['body_dur']:.0f}s・{c['type'] or '未分類'}・{c['score']:.0f} 分）"
                     f"\n     {c.get('video', '')}")
    if args.plan_only:
        lines.append("\n  只產清單，沒有剪。確認後拿掉 --plan-only 再跑一次。")
    _emit({"ok": True, **res}, args.json, "\n".join(lines) + "\n")
    return 0


def cmd_login(args) -> int:
    """存 Pro 授權碼。存在使用者設定目錄，不在程式資料夾裡。"""
    from . import auth

    res = auth.save_token(args.token, feed=args.feed)
    if not res["ok"]:
        _emit(res, args.json, f"\n  {res['error']}")
        return 1
    human = (f"\n  授權碼已存好（{res['masked']}）\n"
             f"  位置：{res['path']}\n"
             + (f"  更新來源：{res['feed']}\n" if res.get("feed") else "")
             + (f"\n  ⚠ {res['warning']}\n" if res.get("warning") else "")
             + "\n  接下來執行 bearcut update 就會拿 Pro 規則包。")
    _emit(res, args.json, human)
    return 0


def cmd_logout(args) -> int:
    """刪掉存起來的授權碼。"""
    from . import auth

    res = auth.clear_token()
    if not res["ok"]:
        _emit(res, args.json, f"\n  {res['error']}")
        return 1
    human = ("\n  已刪掉存起來的授權碼。" if res["existed"]
             else "\n  本來就沒有存授權碼。")
    if res.get("env"):
        # 刪了檔案卻還是有 token，使用者會以為沒登出成功——講清楚是哪裡來的
        human += ("\n  ⚠ 但環境變數 BEARCUT_TOKEN 還設著，更新時仍會用它。"
                  "要完全登出請一併清掉那個環境變數。")
    human += "\n  規則包不受影響，剪片照常。"
    _emit(res, args.json, human)
    return 0


def cmd_ui(args) -> int:
    """開啟本機網頁 UI。"""
    try:
        from .ui import serve
    except ImportError as e:
        print(f"\n  無法啟動介面：缺少必要套件（{e}）\n"
              "  請執行：python bootstrap.py\n")
        return 1
    serve(port=args.port, open_browser=not args.no_browser)
    return 0


def cmd_version(args) -> int:
    data = {"name": "BearCut", "version": __version__,
            "license": "Apache-2.0",
            "copyright": "2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)"}
    _emit(data, args.json, f"BearCut {__version__}  ·  © 川輝科技 Brightstream")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bearcut",
        description="BearCut — 自動順剪，並自己檢查剪得對不對。",
        epilog="第一次使用請先執行：python bootstrap.py",
    )
    p.add_argument("--version", action="version", version=f"BearCut {__version__}")
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("doctor", help="檢查環境是否就緒（缺什麼、怎麼補）")
    d.set_defaults(func=cmd_doctor)

    s = sub.add_parser("setup", help="下載 ffmpeg 到 vendor/（不動系統設定）")
    s.add_argument("--force", action="store_true", help="即使已存在也重新下載")
    s.set_defaults(func=cmd_setup)

    c = sub.add_parser("cut", help="順剪一支影片（辨識 → 偵測 → 剪）")
    c.add_argument("video", nargs="?", help="影片路徑")
    c.add_argument("--plan-only", action="store_true",
                   help="只產待剪清單與對照表，先給人複核，不剪")
    c.add_argument("--apply", metavar="清單.json",
                   help="吃複核過的待剪清單，只執行剪片")
    c.add_argument("--out", metavar="資料夾", help="指定輸出位置")
    c.add_argument("--mode", choices=["fast", "balanced", "precise"],
                   help="效率／平衡／精準（預設 balanced）")
    c.set_defaults(func=cmd_cut)

    sf = sub.add_parser("shortform", help="把剪好的片做成直式短影音（字幕+字卡+封面）")
    sf.add_argument("video", help="影片路徑（通常是 _淨毛片.mp4）")
    sf.add_argument("--srt", help="字幕檔（預設找同名的）")
    sf.add_argument("--title", help="開場標題")
    sf.add_argument("--cta", help="結尾行動呼籲")
    sf.add_argument("--out", metavar="資料夾", help="輸出位置")
    sf.add_argument("--follow", action="store_true", help="追講者裁切（單人時才生效）")
    sf.add_argument("--no-cards", action="store_true", help="不做大字卡")
    sf.add_argument("--no-cover", action="store_true", help="不做封面")
    sf.set_defaults(func=cmd_shortform)

    q = sub.add_parser("verify", help="檢查已剪好的成片（音畫字同步、字幕、接縫畫面）")
    q.add_argument("video", help="成片路徑")
    q.add_argument("--srt", help="字幕檔（預設找同名的）")
    q.add_argument("--frames", action="store_true", help="順便抽接縫畫面出來看")
    q.set_defaults(func=cmd_verify)

    up = sub.add_parser("update", help="更新規則包（門檻與判斷詞，不必重裝程式）")
    up.add_argument("--check-only", action="store_true", help="只看有沒有新版，不安裝")
    up.add_argument("--rollback", action="store_true", help="還原上一版")
    up.add_argument("--feed", help="自訂更新來源（進階規則包用）")
    up.add_argument("--token", help="授權碼（進階規則包用）")
    up.set_defaults(func=cmd_update)

    lf = sub.add_parser("longform", help="長片：一個資料夾的多段影片，順剪後接成一支")
    lf.add_argument("folder", help="放著各段影片的資料夾")
    lf.add_argument("--out", metavar="資料夾", help="輸出位置（預設寫回原資料夾）")
    lf.add_argument("--name", help="成品檔名（預設用資料夾名）")
    lf.add_argument("--mode", choices=["fast", "balanced", "precise"],
                    help="效率／平衡／精準（預設 balanced）")
    lf.add_argument("--force", action="store_true", help="已剪過的段落也重剪")
    lf.set_defaults(func=cmd_longform)

    hl = sub.add_parser("highlights", help="從長片自動挑精華，做成直式短影音（需 Pro 規則包）")
    hl.add_argument("video", help="長片路徑（通常是 _完整淨毛片.mp4）")
    hl.add_argument("--srt", help="字幕檔（預設找同名的）")
    hl.add_argument("--count", type=int, default=3, help="要剪幾支（預設 3）")
    hl.add_argument("--out", metavar="資料夾", help="輸出位置")
    hl.add_argument("--plan-only", action="store_true",
                    help="只挑段落產清單給人複核，先不剪")
    hl.set_defaults(func=cmd_highlights)

    li = sub.add_parser("login", help="貼上 Pro 授權碼（存一次就記住）")
    li.add_argument("token", help="授權碼（信件裡那一串）")
    li.add_argument("--feed", help="自訂更新來源（不給就沿用原本設定）")
    li.set_defaults(func=cmd_login)

    lo = sub.add_parser("logout", help="刪掉存起來的授權碼")
    lo.set_defaults(func=cmd_logout)

    u = sub.add_parser("ui", help="開啟網頁介面（拖放影片、選模式、看進度）")
    u.add_argument("--port", type=int, default=8756)
    u.add_argument("--no-browser", action="store_true", help="不要自動開瀏覽器")
    u.set_defaults(func=cmd_ui)

    v = sub.add_parser("version", help="顯示版本")
    v.set_defaults(func=cmd_version)

    for sp in (d, s, c, sf, q, up, lf, hl, li, lo, u, v):
        sp.add_argument("--json", action="store_true", help="輸出 JSON（給程式/AI Agent 解析）")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
