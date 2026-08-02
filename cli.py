#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# BearCut — © 2026 川輝科技有限公司 (Brightstream Technology Co., Ltd.)
# Licensed under the Apache License, Version 2.0. See LICENSE and NOTICE.
"""根目錄薄殼：讓「解壓縮就直接跑」不必先安裝套件。

實作在 bearcut/cli.py。pip 安裝後請直接用 `bearcut` 指令或 `python -m bearcut`。
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from bearcut.cli import main

if __name__ == "__main__":
    sys.exit(main())
