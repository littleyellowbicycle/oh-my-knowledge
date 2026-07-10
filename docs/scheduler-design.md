# 定时调度器设计 (Scheduler Module)

## 定位

在现有四层认知引擎之上增加**定时触发层**，实现知乎收藏夹/GitHub Trending 等来源的每日自动归档 → 加工 → 索引 → Wiki 编译全流程。

零改动现有模块：调度器只调用已有的 `gateway` / `workflow` / `raw_store` 公开接口。

## 架构

```
scheduler/
├── __init__.py
├── config.py            # 声明式任务配置
├── tasks.py             # 任务函数（可被任何触发器调用）
└── daemon.py            # APScheduler 内嵌守护（可选）
```

### config.py — 声明式任务配置

```python
from dataclasses import dataclass, field

ZHIHU_COLLECTIONS: list[str] = [
    # "https://www.zhihu.com/collection/xxx",
]

TRENDING_LANGUAGES: list[str] = ["python", "typescript", "rust"]
TRENDING_SINCE: str = "daily"  # daily / weekly

# craw4ai / gh CLI 抓取超时
FETCH_TIMEOUT: int = 60  # seconds
```

用户只需编辑这个文件即可增删任务，不改逻辑。

### tasks.py — 可复用任务

所有任务函数返回 `TaskResult`：

```python
@dataclass
class TaskResult:
    name: str
    success: bool
    entries: int       # 新增 raw 条目数
    duration: float    # 耗时秒数
    error: str | None
```

**内置任务：**

| 函数 | 用途 | 复用组件 |
|------|------|---------|
| `fetch_zhihu_collections()` | 遍历配置中的收藏夹 URL，逐篇 ingest | `gateway.fetch_url()`, `raw_store.save_raw()` |
| `fetch_github_trending()` | 爬 GitHub Trending 落盘 raw/ | `requests` + `bs4` 解析, `raw_store.save_raw()` |
| `fetch_url_list(urls)` | 通用 URL 列表批量抓取 | `gateway.fetch_url()` |
| `run_pipeline()` | 加工所有 pending → 重建索引 → 编译 Wiki | `workflow.run_pipeline()` |
| `run_daily()` | 组合上述所有任务（知乎+Trending+加工+索引+Wiki） | 组合调用 |
| `weekly_report()` | 生成本周入库摘要（🔜） | `indexer.summaries` |

## 数据流

```
定时触发 (如 08:00 每日)
  │
  ├── Task 1: fetch_zhihu_collections()
  │   └── for each URL → gateway.fetch_url() → raw_store.save_raw()
  │
  ├── Task 2: fetch_github_trending()
  │   └── requests + bs4 → raw_store.save_raw()
  │
  ├── Task 3: run_pipeline()
  │   ├── process pending raw → processed/
  │   ├── compute_relations (双向双链)
  │   ├── rebuild_index
  │   └── compile wikis (关联簇 ≥3)
  │
  └── [可选] 通知: 打印摘要 / webhook / Obsidian 推送
```

## 四种触发方案

### 方案 A: APScheduler 内嵌 (推荐本地开发)

`daemon.py` 内嵌到 `kb serve` 中，进程启动后自动注册定时任务。

```python
from apscheduler.schedulers.background import BackgroundScheduler

scheduler = BackgroundScheduler()
scheduler.add_job(run_daily, "cron", hour=8, minute=0, id="daily_pipeline")
scheduler.start()
```

**优点：** 零外部依赖，一行 `kb serve` 即启动
**缺点：** `kb serve` 必须常驻后台

### 方案 B: Windows Task Scheduler (推荐生产)

创建 XML 任务定义，一次导入：

```xml
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4">
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>2026-07-10T08:00:00</StartBoundary>
      <Repetition><Interval>PT24H</Interval></Repetition>
    </CalendarTrigger>
  </Triggers>
  <Actions>
    <Exec>
      <Command>python</Command>
      <Arguments>src/cli.py workflow</Arguments>
      <WorkingDirectory>D:\project\my-knowledge-base</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
```

**优点：** 系统级可靠，不占用终端
**缺点：** 仅 Windows

### 方案 C: Lobster cron (MCP 触发)

如果 Lobster 支持定时调用 MCP tools，配置类似：

```json
{
  "cron": [
    {
      "schedule": "0 8 * * *",
      "task": "mcp:oh-my-knowledge/run_pipeline"
    }
  ]
}
```

**优点：** 可编排复杂多步逻辑，带日志
**缺点：** 依赖 Lobster 运行

### 方案 D: GitHub Actions (云端)

`.github/workflows/daily-ingest.yml`：

```yaml
on:
  schedule:
    - cron: "0 0 * * *"
  workflow_dispatch:

jobs:
  ingest:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install -r requirements.txt
      - run: python src/cli.py workflow
```

**优点：** 免费、日志可查、Webhook 可触发
**缺点：** 需要公开或 GitHub 托管仓库

## 与现有组件的关系

| 组件 | 关系 |
|------|------|
| `src.gateway` | `tasks.py` 直接调用 `gateway.fetch_url()` |
| `src.raw_store` | `tasks.py` 直接调用 `raw_store.save_raw()` |
| `src.workflow` | `tasks.py` 直接调用 `workflow.run_pipeline()` |
| `src.cli` | 新增 `kb schedule` 子命令: 列出/启用/禁用任务 |
| `docs/trend-to-copy-design.md` | `fetch_github_trending()` 对接 trend-to-copy 选题源 |

## 后续可扩展

1. **失败重试**：TaskResult 中记录错误，可配置重试策略
2. **通知渠道**：成功后 Webhook → Discord / 飞书 / 钉钉
3. **Web 管理页**：FastAPI 路由 `/scheduler` 查看任务状态
4. **增量 Trend 监控**：Notion-like 数据库记录已抓过的 repo，避免重复
