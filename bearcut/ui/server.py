# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""本機網頁 UI。

## 為什麼是網頁而不是桌面視窗

- **跨平台一致**：tkinter 在 macOS 的外觀與行為跟 Windows 差很多，要各調一次
- **拖放影片自然**：桌面工具包的拖放支援普遍很差
- **看得到進度**：剪片要好幾分鐘，使用者需要知道現在在做什麼、還要多久

## 設計原則：一個畫面做完一件事

不做分頁、不做設定頁。拖影片進來 → 選快或準 → 按一顆按鈕 → 看進度 → 拿結果。
所有選項都用使用者的語言（「效率模式」而不是 `model_size=medium`）。

## 只綁 127.0.0.1

這是本機工具，不是伺服器。綁定 localhost 讓它不會意外暴露在區網上——
使用者的影片是隱私。
"""

import json
import os
import queue
import threading
import uuid
import webbrowser
from pathlib import Path
from typing import Dict, Optional

from ..env.platform import ROOT, console_utf8
from ..rules import DEFAULT_PROFILE, profiles

STATIC = Path(__file__).resolve().parent / "static"

# 每個任務的狀態。單機工具，記憶體存放就夠——不需要資料庫。
_jobs: Dict[str, dict] = {}


class Job:
    """一支影片的處理任務。"""

    def __init__(self, video: str, mode: str, plan_only: bool = False,
                 kind: str = "cut", opts: Optional[dict] = None):
        self.id = uuid.uuid4().hex[:12]
        self.video = video
        self.mode = mode
        self.plan_only = plan_only
        self.kind = kind                  # "cut" 或 "shortform"
        self.opts = opts or {}
        self.percent = 0
        self.message = "準備中…"
        self.done = False
        self.error: Optional[str] = None
        self.result: Optional[dict] = None
        self.log: list = []

    def report(self, p, m):
        self.percent = max(0, min(100, int(p)))
        self.message = m
        self.log.append(m)
        del self.log[:-200]          # 只留最近 200 行，避免長片吃記憶體

    def to_dict(self) -> dict:
        return {"id": self.id, "percent": self.percent, "message": self.message,
                "done": self.done, "error": self.error, "result": self.result,
                "log": self.log[-40:],
                "video": os.path.basename(self.video), "mode": self.mode}


def _run_job(job: Job) -> None:
    """在背景執行緒跑剪片或短影音。"""
    try:
        if job.kind == "shortform":
            from ..shortform import make
            res = make(job.video, title=job.opts.get("title"),
                       cta=job.opts.get("cta"),
                       use_cards=job.opts.get("cards", True),
                       follow_speaker=job.opts.get("follow", False),
                       progress_cb=job.report)
            job.result = {
                "summary": {"大字卡": f"{len(res.get('cards') or [])} 張"},
                "files": {k: v for k, v in res.items() if isinstance(v, str)},
                "folder": os.path.dirname(os.path.abspath(job.video)),
            }
            job.percent = 100
            job.message = "完成"
            return

        from ..pipeline import analyze
        res = analyze(job.video, do_cut=not job.plan_only,
                      profile=job.mode, progress_cb=job.report)
        job.result = {
            "summary": res["plan_data"]["summary"],
            "files": {k: v for k, v in res.items()
                      if k not in ("plan_data", "frames", "retest", "qa_issues")},
            "issues": res.get("qa_issues") or [],
            "cuts": res["plan_data"]["cuts"][:60],
            "keep": res["plan_data"]["keep"],
            "duration": res["plan_data"]["duration_sec"],
            "folder": os.path.dirname(os.path.abspath(job.video)),
        }
        job.percent = 100
        job.message = "完成"
    except Exception as e:
        # 錯誤要讓使用者看得懂，不是丟 traceback
        job.error = str(e) or type(e).__name__
        job.message = "發生問題"
    finally:
        job.done = True


def create_app():
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import FileResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    from pydantic import BaseModel

    app = FastAPI(title="BearCut", docs_url=None, redoc_url=None)

    class StartReq(BaseModel):
        video: str
        mode: str = DEFAULT_PROFILE
        plan_only: bool = False
        kind: str = "cut"
        title: Optional[str] = None
        cta: Optional[str] = None
        cards: bool = True
        follow: bool = False

    @app.get("/")
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/info")
    def info():
        """啟動時給前端的基本資料：模式選項、環境狀態。"""
        from ..env import doctor
        from .. import __version__
        chk = doctor.check()
        return {
            "version": __version__,
            "profiles": profiles(),
            "default_profile": DEFAULT_PROFILE,
            "ready": chk["ok"],
            "blocking": chk["blocking"],
            "checks": {k: {"ok": v["ok"], "detail": v["detail"], "fix": v["fix"]}
                       for k, v in chk["checks"].items()},
            "warnings": chk.get("warnings", []),
            "gpu": chk["platform"]["gpu"]["detail"],
            "llm": chk["llm"]["note"],
        }

    @app.post("/api/start")
    def start(req: StartReq):
        if not os.path.exists(req.video):
            raise HTTPException(400, f"找不到影片：{req.video}")
        job = Job(req.video, req.mode, req.plan_only, kind=req.kind,
                  opts={"title": req.title, "cta": req.cta,
                        "cards": req.cards, "follow": req.follow})
        _jobs[job.id] = job
        threading.Thread(target=_run_job, args=(job,), daemon=True).start()
        return {"id": job.id}

    @app.get("/api/job/{job_id}")
    def job_status(job_id: str):
        job = _jobs.get(job_id)
        if not job:
            raise HTTPException(404, "找不到這個任務")
        return job.to_dict()

    class LoginReq(BaseModel):
        token: str
        feed: Optional[str] = None

    @app.get("/api/auth")
    def auth_status():
        """目前的授權狀態。只回遮罩過的字串，不回完整授權碼。"""
        from .. import auth
        return auth.status()

    @app.post("/api/auth")
    def auth_login(req: LoginReq):
        """存授權碼。走的是 CLI 那支同一個 save_token()，兩邊行為不會分岔。"""
        from .. import auth
        res = auth.save_token(req.token, feed=req.feed)
        if not res["ok"]:
            raise HTTPException(400, res["error"])
        return res

    @app.delete("/api/auth")
    def auth_logout():
        from .. import auth
        res = auth.clear_token()
        if not res["ok"]:
            raise HTTPException(500, res.get("error") or "刪不掉設定檔")
        return res

    @app.post("/api/open")
    def open_folder(body: dict):
        """在檔案總管 / Finder 裡打開輸出資料夾。

        使用者剪完最常做的下一件事就是去看檔案，這一步幫他省掉。
        """
        path = body.get("path") or ""
        if not os.path.isdir(path):
            raise HTTPException(400, "資料夾不存在")
        import subprocess
        import sys
        try:
            if sys.platform.startswith("win"):
                os.startfile(path)                       # noqa: S606
            elif sys.platform == "darwin":
                subprocess.run(["open", path], check=False)
            else:
                subprocess.run(["xdg-open", path], check=False)
        except Exception as e:
            raise HTTPException(500, f"無法開啟資料夾：{e}")
        return {"ok": True}

    app.mount("/static", StaticFiles(directory=str(STATIC)), name="static")
    return app


def serve(host: str = "127.0.0.1", port: int = 8756, open_browser: bool = True) -> None:
    """啟動 UI。"""
    console_utf8()
    import uvicorn

    url = f"http://{host}:{port}"
    print(f"\n  BearCut 已啟動：{url}")
    print("  關掉這個視窗就會停止。\n")
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()

    # log_level="warning"：使用者不需要看到每個 HTTP 請求
    uvicorn.run(create_app(), host=host, port=port, log_level="warning")
