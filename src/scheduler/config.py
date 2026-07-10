"""声明式任务配置 — 用户编辑此文件即可增删定时任务。

所有配置支持 .env 环境变量覆盖，优先级: .env > 本文件默认值。
"""

from __future__ import annotations

import os

# ---- 知乎收藏夹 ----
# 每日自动抓取的收藏夹 URL 列表（需配套 cookies_zhihu.json）
ZHIHU_COLLECTIONS: list[str] = [
    # "https://www.zhihu.com/collection/xxx",
]

# ---- GitHub Trending ----
TRENDING_LANGUAGES: list[str] = ["python", "typescript", "rust"]
TRENDING_SINCE: str = "daily"  # daily / weekly

# ---- 调度时间 ----
# 每日定时执行的时分（24h 制），可通过 .env 覆盖
DAILY_HOUR: int = int(os.getenv("SCHEDULER_DAILY_HOUR", "8"))
DAILY_MINUTE: int = int(os.getenv("SCHEDULER_DAILY_MINUTE", "0"))

# ---- 抓取超时 ----
FETCH_TIMEOUT: int = int(os.getenv("SCHEDULER_FETCH_TIMEOUT", "60"))

# ---- 时区 ----
TIMEZONE: str = os.getenv("SCHEDULER_TIMEZONE", "Asia/Shanghai")
