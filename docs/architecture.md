
# 🏗️ 架构设计 V2.1 — 四层认知引擎 (Three-Tier Cognitive Engine)

## 0. 设计原则

| 原则       | 含义                                                                    |
| ---------- | ----------------------------------------------------------------------- |
| 四层隔离   | 原料层不可变、加工层可迭代、索引层可重建                                |
| 文件优先   | MVP 阶段零数据库依赖，全靠文件系统 + JSON                               |
| LLM 无关   | 通过 LiteLLM  SDK 模式屏蔽本地 Ollama 与云端差异                        |
| 单向流动   | 数据流向 Raw → Processed → Wiki → Index，不可反向污染                   |
| 确定性优先 | 关联关系由确定性算法计算（标签+标题分词打分），LLM 仅负责内容生成与重组 |
---

## 1. 系统全景图

```
┌─────────────────────────────────────────────────────────────┐
│                  输入网关 (Gateway — 通道分层架构)            │
│  ┌──────────┐  ┌──────────────────────────┐  ┌──────────┐  │
│  │ 手动输入  │  │ Channel Router (瘦路由)  │  │ V2/V3源  │  │
│  │          │  │  URL → match() → channel │  │ 定时/API │  │
│  │          │  │                          │  │ 文件/音视│  │
│  └─────┬────┘  └────────────┬─────────────┘  └─────┬────┘  │
│        │           ┌────────┼────────┐              │       │
│        │           ▼        ▼        ▼              │       │
│        │      ┌──────┐ ┌──────┐ ┌──────┐ ┌──────┐  │       │
│        │      │GitHub│ │知乎   │ │微信   │ │通用   │  │       │
│        │      │Channel│ │Channel│ │Channel│ │Channel│  │       │
│        │      │gh CLI│ │API+  │ │Jina  │ │Crawl4│  │       │
│        │      │      │ │Crawl4│ │Reader│ │AI兜底│  │       │
│        │      └──┬───┘ └──┬───┘ └──┬───┘ └──┬───┘  │       │
│        └─────────┼────────┼────────┼────────┼──────┘       │
│                  └────────┼────────┴────────┘              │
│                           ▼                                 │
│             [统一归一化入口 normalize()]                     │
└─────────────────────────────┬───────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   四层存储 (Storage)                         │
│  ┌──Raw──┐  ┌──Processed──┐  ┌──Wiki(B4)⭐──┐  ┌──Index──┐ │
│  │只读不变│→│AI结构化产出 │→│LLM编译综述   │→│MOC/标签/│ │
│  │Hash命名│ │JSON Mode    │ │关联簇≥3触发  │ │反向链接 │ │
│  │原始格式│ │核心结论+双链│ │双链确定性覆盖│ │可重建   │ │
│  └────────┘ └─────────────┘ └──────────────┘ └─────────┘ │
│   Raw ──(LLM加工)──▶ Processed ──(关联簇触发)──▶ Wiki      │
│   Processed + Wiki ──(提取)──▶ Index                       │
└─────────────────────────────┬───────────────────────────────┘
                              ▼
┌─────────────────────────────────────────────────────────────┐
│                   输出引擎 (Output Engine, 只读)            │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐                  │
│  │问答流 QA │  │选题流    │  │周报流    │                  │
│  │查索引→读 │  │聚合主题→ │  │审计变更→ │                  │
│  │结论/Wiki │  │反推断裂  │  │汇总      │                  │
│  └──────────┘  └──────────┘  └──────────┘                  │
└─────────────────────────────────────────────────────────────┘
```

---

## 2. 模块拆分与职责

### 模块 A：输入网关 (Gateway — 通道分层架构)
职责：接收一切来源，按平台路由分发抓取，归一化为统一格式后送入原料层。

#### A.1 通道分层设计 (V2.2 重构)

网关采用**通道 (Channel) 插件化架构**，每个平台一个独立通道文件，路由器零逻辑遍历通道列表：

```
src/gateway/
├── __init__.py            # 对外暴露 fetch_url() / fetch_manual() / is_expandable()
├── router.py              # 瘦路由: 遍历 channels，第一个 match() 命中的负责处理
├── base.py                # Channel 协议 (match + fetch + 可选 fetch_items)
└── channels/
    ├── __init__.py        # 自动注册所有通道 (按优先级排序)
    ├── github.py          # gh CLI + REST API
    ├── zhihu.py           # 收藏夹 items API + Crawl4AI 单篇
    ├── weixin.py          # Jina Reader
    ├── generic.py         # Crawl4AI 兜底 (永远最后匹配)
    └── xiaohongshu.py     # 未来扩展
```

#### A.2 Channel 协议

```python
class Channel(Protocol):
    name: str                              # "zhihu" / "weixin" / "github"
    def match(self, url: str) -> bool      # 这个 URL 归我管吗？
    def fetch(self, url: str, cookies=None) -> str          # 抓取 → markdown
    def fetch_items(self, url: str, cookies=None) -> list[dict] | None
        # 可选: 展开型通道 (收藏夹)。返回 [{url, title}] 或 None (不支持展开)
```

#### A.3 Cookie 传递机制

Cookie 优先级 (从高到低):
1. **显式参数**: `fetch_url(url, cookies={"z_c0":"..."})` — 一次性传入
2. **通道专属文件**: `cookies_zhihu.json` — 各通道独立读取
3. **全局文件**: `cookies.json` — 通用兜底
4. **无 cookie**: 公开内容能抓则抓，需登录则返回清晰提示

#### A.4 抓取策略分层

| 通道 | 列表/索引 | 单篇正文 | Cookie 需求 |
| ---- | --------- | -------- | ----------- |
| GitHub | — | gh CLI / REST API | 可选 (提速率限制) |
| 知乎 | requests + items API (JSON) | Crawl4AI (Playwright+cookie注入) → requests+bs4 → API 兜底 | 必须 (items API 401) |
| 微信 | — | Jina Reader API → Crawl4AI | 免 cookie |
| 小红书 | requests + API | Crawl4AI (重JS+cookie) | 必须 |
| 通用 | — | Crawl4AI → requests | 可选 |

**关键原则**:
- JSON API 用 requests (轻量快速)，JS 渲染页面用 Crawl4AI (浏览器渲染)
- 展开型通道 (如知乎收藏夹): 先 API 拿列表 → 逐篇 Crawl4AI 抓正文
- 每个通道内部自行选择最优工具组合，路由器不关心细节

#### A.5 新增平台流程

只需在 `channels/` 下加一个文件实现 Channel 协议，在 `__init__.py` 注册。**不改动任何现有代码** (开闭原则)。


归一化后的数据结构 (Raw Entry):
```
{
  "id": "raw_20240115_143022_a3f1",   # 时间戳 + 短 hash
  "source_type": "link | manual | api | cron | file",
  "source_url": "https://...",         # 可选
  "original_text": "...",              # 原始全文
  "ingested_at": "2024-01-15T14:30:22",
  "status": "pending"                  # pending → processed → indexed
}
```

---

### 模块 B1：原料层
职责：事实来源，只读归档。

目录结构:
```
my_kb/
└── raw/
    ├── raw_20240115_143022_a3f1.md    # 手动想法
    |── raw_20240115_143022_a3f1.txt
    └── raw_20240115_143022_a3f1.meta.json
```

规则:
- 文件名: `raw_{timestamp}_{short_hash}.txt`
- 元数据: 同名 `.meta.json` 记录来源、URL、时间、状态
- 不可变: 写入后只读，不允许修改
- 保留原始格式: 不做任何清洗，原样保存

---

### 模块 B2：加工层
职责：AI 结构化产出，可迭代修改，Obsidian Vault 挂载点。

目录结构:
```
my_kb/
└── processed/
    ├── AI Agent 架构设计.md
    ├── 知识碎片化管理方法论.md
    └── ...
```

每篇笔记的标准化 Markdown 结构:
```markdown
---
title: "AI Agent 架构设计"
source: "raw_20240115_143022_a3f1"
source_url: "https://..."
created: "2024-01-15"
updated: "2024-01-15"
tags: [AI, Agent, 架构]
status: "processed"
---

# AI Agent 架构设计

## 核心结论
> (2-3 句话直接给结论，这是问答流的唯一读取入口)

## 详细内容
正文重写，包含 [[双链概念]] 标记...

## 相关笔记
- [[知识碎片化管理方法论]]
- [[向量检索原理]]
```

Frontmatter 字段说明:
| 字段       | 类型     | 含义                          |
| ---------- | -------- | ----------------------------- |
| title      | string   | 笔记标题                      |
| source     | string   | 对应原料层文件 ID             |
| source_url | string   | 原始链接 (可选)               |
| created    | date     | 创建日期                      |
| updated    | date     | 最后修改日期                  |
| tags       | string[] | 标签列表                      |
| status     | enum     | pending / processed / indexed |

规则:
- `## 核心结论` 是硬约束：必须有，且不超过 3 句话
- `## 详细内容` 中必须使用 `[[]]` 标记关键概念
- 用户可随时手动修改加工层笔记，修改不影响原料层

---

### 模块 B3：索引层
职责：导航目录，快速定位，可从加工层完全重建。

目录结构:
```
my_kb/
└── index/
    ├── moc.json           # 全局 MOC (Map of Content) 目录树
    ├── tags.json          # 标签反向索引
    └── summaries.json     # 所有笔记的摘要卡片
```

索引文件格式:

moc.json (主题目录):
```json
{
  "topics": {
    "AI Agent": {
      "notes": ["AI Agent 架构设计.md", "Agent 框架对比.md"],
      "subtopics": ["Multi-Agent", "Tool Use"]
    },
    "知识管理": {
      "notes": ["知识碎片化管理方法论.md"],
      "subtopics": []
    }
  },
  "last_rebuilt": "2024-01-15T15:00:00"
}
```

tags.json (标签反向索引):
```json
{
  "AI": ["AI Agent 架构设计.md", "Agent 框架对比.md"],
  "架构": ["AI Agent 架构设计.md"],
  "知识管理": ["知识碎片化管理方法论.md"]
}
```

summaries.json (摘要卡片):
```json
{
  "AI Agent 架构设计.md": {
    "title": "AI Agent 架构设计",
    "conclusion": "Agent 架构核心在于感知-决策-执行闭环...",
    "tags": ["AI", "Agent", "架构"],
    "links": ["Multi-Agent", "Tool Use", "向量检索"]
  }
}
```

规则:
- 索引层是衍生数据，任何时候都可以删掉从加工层重建
- 每次加工层新增/修改笔记时，增量更新索引
- 重建命令: `rebuild_index()` 扫描全部 processed/ 重新生成

### 模块 B4：编译层 Wiki 
职责：(Karpathy 理念落地)
以 atomicstrata/llm-wiki-compiler 的两阶段编译流水线和 Prompt 设计为蓝图。
以 Hosuke/llmwiki (Python 包) 作为运行时基座。
自研胶水层：实现原料格式转换、触发机制、以及双链覆盖。
触发条件：当 B2 层某一主题的关联簇（通过确定性打分得出）笔记数 ≥ 3 时，自动触发编译。
双链覆盖：LLM 编译生成 Wiki 综述后，Python 胶水层在文末自动追加 ## 相关笔记（确定性关联） 模块，覆盖 LLM 自由生成的悬空双链。

---

### 模块 S：定时调度器 (V2)

职责：定时触发归档任务，复用 Gateway + Workflow 公开接口，零改动现有模块。

```
scheduler/
├── config.py    # 声明式任务配置（用户编辑）
├── tasks.py     # 任务函数（可被任何触发器调用）
└── daemon.py    # APScheduler 内嵌守护（可选）
```

详见 `docs/scheduler-design.md`。

### 模块 C：输出引擎

#### C1: 问答流
```
用户提问
  │
  ▼
[1] 查索引层 summaries.json
    → 用 LLM 对所有 summary 做相关性判断 (MVP: 全量塞入 prompt)
    → 选出 Top-3 最相关笔记
  │
  ▼
[2] 读加工层对应笔记的 ## 核心结论 段落
    (不读全文，只读结论)
  │
  ▼
[3] LLM 综合结论 + 用户问题 → 生成回答
    附带来源笔记链接
```

关键约束:
- 禁止回头读原料层原文
- 禁止读详细内容，只读核心结论
- 回答必须标注来源笔记名

---

#### C2: 选题流
```
用户输入主题 (或自动扫描索引层热门主题)
  │
  ▼
[1] 从 moc.json 中取出该主题下所有笔记标题 + 标签
  │
  ▼
[2] LLM 分析:
    - 哪些笔记之间有逻辑断裂 → 可填补的选题
    - 哪些笔记有互补关系 → 可整合的选题  
    - 哪些方向只有单篇笔记 → 可深化的选题
  │
  ▼
[3] 输出选题清单:
    选题标题 + 2句话理由 + 涉及笔记链接
```

---

#### C3: 周报流
```
触发: 每周定时 / 手动命令
  │
  ▼
[1] 扫描原料层: 本周新增的 raw 文件 (按 ingested_at 过滤)
  │
  ▼
[2] 扫描加工层: 本周新建/修改的 processed 文件 (按 created/updated 过滤)
  │
  ▼
[3] 扫描索引层: 
    - 哪些主题笔记密度增加 → "该编译了"
    - 哪些主题还没有 MOC → "建议建立目录"
  │
  ▼
[4] LLM 汇总生成周报 Markdown:
    ## 本周入库 (N 篇)
    ## 主题动态
    ## 建议编译的主题
```

---

## 3. 模块间接口定义

```
normalize(raw_input) → RawEntry              # Gateway → Raw
process(RawEntry) → ProcessedNote            # Raw → Processed (经 LLM + JSON Mode)

# --- 输入网关接口 (V2.2 通道分层) ---
fetch_url(url, cookies=None) → str           # 瘦路由 → channel.fetch()
fetch_zhihu_collection_items(url, cookies=None) → list[dict]  # 展开型: 返回文章列表
save_collection(url) → list[RawEntry]        # 收藏夹 → 多篇独立 raw 条目
# Channel 协议: match(url) → bool | fetch(url, cookies) → str | fetch_items(url, cookies) → list|None
compute_relations(new_note) → RelatedList    # Processed 内部 (确定性打分)
index_note(ProcessedNote) → IndexUpdate      # Processed → Index
compile_wiki(topic) → WikiPage               # Processed → Wiki (经 llmwiki + 胶水层) ⭐
lint_wiki() → HealthReport                   # Wiki 自检 (对应 Karpathy Lint)
rebuild_index() → void                       # 全量重建索引
rebuild_wiki() → void                        # 全量重新编译
on_processed_updated(note) → void            # B2 变更回调 → 触发 B4 增量编译
qa(question) → Answer + sources              # Index + Processed + Wiki → Answer
suggest_topics(topic?) → TopicList           # Index → TopicList (🔜 V2)
weekly_report() → Markdown                   # 全层审计 → Report (🔜 V2)
```

---

## 4. 数据生命周期

```
新建:
  input → normalize → raw/ (pending)
         → LLM process (JSON Mode) → processed/ (processed)
         → compute_relations → 双向写入 related
         → extract → index/ (indexed)
         → 若关联簇 ≥3 → compile_wiki → wiki/ (compiled)

修改:
  用户手动编辑 processed/*.md
  → 检测变更 → 增量更新 index/
  → 触发受影响主题簇增量编译 wiki/ (llmwiki SHA-256 跳过未变更)
  → raw/ 不受影响

删除:
  用户删除 processed/*.md
  → 同步删除 index/ 对应条目
  → 触发相关 wiki/ 重新编译或标记失效
  → raw/ 保留 (原料层是事实档案)

重建:
  删除 index/ → 扫描 processed/ + wiki/ → 重新生成全部索引
  删除 wiki/ → 扫描 processed/ 关联簇 → 全量重新编译
```

---

## 5. MVP vs 完整版 对照

维度	MVP (当前)	V2	V3
输入源	手动+链接(多平台路由)	+ 定时拉取 + API 推送	+ PDF + 图片 + 音视频
存储	纯文件系统四层	纯文件系统	+ SQLite + 向量库
检索	LLM 全量 prompt 判断	标签索引 + LLM 辅助	向量语义检索
索引	JSON 文件 (含 links.json)	+ 增量更新	向量库 + 图数据库
Wiki 编译 ⭐	llmwiki + 胶水层 (手动触发)	+ 增量自动编译	+ 多页合并 + 概念图谱
关联	确定性加权打分	+ LLM 辅助补全	向量语义关联
输出	问答流 (C1)	+ 选题流 (C2) + 周报 (C3)	+ 知识图谱可视化
前端	CLI + Obsidian(挂载 processed/+wiki/)	同左	Web UI
---

## 6. 技术栈锁定 (MVP)

层 / 模块	技术	角色 / 形态	选型理由
语言	Python 3.11+	主控语言	LLM 生态最完善
Web 框架	FastAPI	后续 Webhook 用	异步、轻量、自带文档
LLM 适配	LiteLLM  SDK 模式	Ollama 兼容 OpenAI 接口，一套代码双端通用
结构化输出	JSON Mode + Pydantic	加工引擎约束	保证合法 JSON，代码端强校验拼装 MD
网页抓取-主	Crawl4AI	运行时依赖	HTTP/Playwright 自适应，通吃知乎/小红书/博客
网页抓取-微信	Jina Reader API	外部云服务	绕过公众号反爬，免维护 Cookie
网页抓取-GitHub	gh CLI	系统工具调用	官方 API 路径，稳定防限流
中文分词	jieba	运行时依赖	确定性关联打分基础
Markdown 解析	python-frontmatter	运行时依赖	稳定读写 YAML Frontmatter
双链提取	内置 re 正则	代码内逻辑	极简提取 [[]]，构建 links.json
定时调度	APScheduler (可选)	runtime 依赖	内嵌到 kb serve 实现定时触发 (见 scheduler-design.md)
Wiki 编译基座	llmwiki (Hosuke)	pip 包引入	Python 原生、支持 CJK、无向量库 pypi.org
Wiki 编译蓝图	llm-wiki-compiler	参考实现	借鉴两阶段流水线、SHA-256 增量、Claim 溯源 csdn.net
胶水层逻辑	自研 Python 模块	代码内逻辑	格式转换/触发判定/双链确定性覆盖
文件存储	pathlib / os	本地文件系统	零依赖、可 Git
索引存储	JSON 文件	本地文件系统	人类可读，可随时重建
前端	Obsidian	挂载 processed/ + wiki/	双链图谱开箱即用


## 流程图
```mermaid
graph TD
    classDef gateway fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
    classDef raw fill:#ffebee,stroke:#b71c1c,stroke-width:2px;
    classDef processed fill:#e8f5e9,stroke:#1b5e20,stroke-width:2px;
    classDef wiki fill:#fce4ec,stroke:#880e4f,stroke-width:2px,stroke-dasharray: 5 5;
    classDef index fill:#fff3e0,stroke:#e65100,stroke-width:2px;
    classDef scheduler fill:#fff8e1,stroke:#f57f17,stroke-width:2px;
    classDef output fill:#f3e5f5,stroke:#4a148c,stroke-width:2px;
    classDef llm fill:#eeeeee,stroke:#424242,stroke-dasharray: 5 5;

    subgraph Scheduler [模块 S: 定时调度器 V2]
        S1[APScheduler / Task Scheduler<br/>每日 08:00 触发]:::scheduler
        S2[tasks.py<br/>fetch_zhihu / fetch_trending<br/>run_pipeline]:::scheduler
        S1 --> S2
    end

    subgraph Gateway [模块 A: 输入网关 — 通道分层]
        A1[手动输入/想法]:::gateway
        Router[Channel Router<br/>URL → match() → channel]:::gateway
    A1 --> Router
    S2 -->|定时调用| Router
    A3[Scheduler 触发]:::gateway
    A4[文件解析/音视频 V3]:::gateway
    Router -->|归一化| Normalize(统一入口 normalize):::gateway
    end

    subgraph Channels [通道插件]
        Ch1[GitHub Channel<br/>gh CLI / REST API]:::gateway
        Ch2[知乎 Channel<br/>items API + Crawl4AI]:::gateway
        Ch3[微信 Channel<br/>Jina Reader]:::gateway
        Ch4[通用 Channel<br/>Crawl4AI 兜底]:::gateway
    end

    Router -.->|match| Ch1
    Router -.->|match| Ch2
    Router -.->|match| Ch3
    Router -.->|fallback| Ch4
    Ch1 & Ch2 & Ch3 & Ch4 -.->|markdown| Normalize

    LLM((LLM Adapter<br/>Ollama/OpenAI<br/>JSON Mode)):::llm

    subgraph Storage [模块 B: 四层存储架构 V2.1]
        B1[原料层 Raw<br/>只读不可变<br/>Hash 命名]:::raw
        B2[加工层 Processed<br/>可迭代修改<br/>核心结论+双链<br/>确定性关联打分<br/>Obsidian 挂载]:::processed
        B4[编译层 Wiki ⭐<br/>LLM 编译综述<br/>关联簇≥3 触发<br/>双链确定性覆盖<br/>llmwiki+胶水层]:::wiki
        B3[索引层 Index<br/>可随时重建<br/>MOC+标签+links.json]:::index
        
        B1 -->|AI 加工| LLM
        LLM -->|结构化 MD| B2
        B2 -->|关联簇 ≥3 触发| B4
        B2 -->|Frontmatter/正则提取| B3
        B4 -->|提取 Wiki 元数据| B3
    end

    subgraph Output [模块 C: 输出引擎 只读]
        C1[问答流 QA<br/>查索引→读结论/Wiki]:::output
        C2[选题流 Topic<br/>聚合主题→反推断裂]:::output
        C3[周报流 Weekly V2<br/>审计全层变更→汇总]:::output
    end

    Normalize -->|写入| B1
    B3 -.->|定位路径| C1
    C1 -.->|只读结论| B2
    C1 -.->|只读综述| B4
    B3 -.->|读取 MOC| C2
    B1 -.->|统计入库| C3
    B2 -.->|统计更新| C3
    B4 -.->|统计编译| C3
    B3 -.->|统计密度| C3

    Note1>原则: 单向流动[Raw→Processed→Wiki→Index]:::llm
    Note2>原则: [四层隔离 + 确定性优先]:::llm
```