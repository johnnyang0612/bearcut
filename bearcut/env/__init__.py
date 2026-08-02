# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0.
"""環境層：平台偵測、外部工具取得、自檢。

本子套件**只用標準函式庫**，因為 bootstrap.py 會在相依套件裝好之前 import 它。
新增檔案到這裡時請維持這個限制。
"""

from . import doctor, ffmpeg, platform  # noqa: F401
