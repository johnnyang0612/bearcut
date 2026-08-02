# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0.
"""讓 `python -m bearcut` 與 pip 安裝後的 `bearcut` 指令都走同一個入口。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
