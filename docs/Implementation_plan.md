# 🗺️ 四层认知引擎 - 实施路线图

## 实施进度

| Step | 内容 | 状态 |
|------|------|------|
| Step 0 | 项目基建与 LiteLLM 适配层 | ✅ |
| Step 1 | 输入网关与原料层（多平台路由） | ✅ |
| Step 1.5 | 网关通道分层重构 (V2.2 Channel 插件化) | ✅ |
| Step 2 | 加工引擎与结构化产出（JSON Mode + Pydantic） | ✅ |
| Step 3 | 确定性关联引擎（标签 + 标题分词打分） | ✅ |
| Step 4 | 索引层与 QA 问答流 | ✅ |
| Step 5 | Wiki 编译层（Karpathy 理念 + 连通分量） | ✅ |
| Step 6 | FastAPI 服务与 Obsidian 对接 | ✅ |
| Step 7 | MCP 服务（供 AI Agent 标准协议调用） | ✅ |
| Step 8 | 两步走分层问答（Wiki 优先 → 降级 Processed） | ✅ |
| Step 9 | 定时调度器 (Scheduler 模块) | 🔜 |
| Step 10 | 音视频处理 (PDF/图片/音视频) | 🔜 |
| Step 11 | 多学生协作与学习分析 | 🔜 |

本文档基于定稿的 V2.1 架构（含确定性打分、LiteLLM 国产模型适配、Karpathy Wiki 编译理念）编制。开发遵循"自底向上、先跑通主链路再叠加智能化"的原则。

---

## Step 0: 项目基建与 LLM 适配层 (LiteLLM 集成)
* **目标**：搭建四层目录骨架，通过 LiteLLM SDK 跑通国产大模型（GLM/DeepSeek/Kimi等）的统一调用。
* **动作**：
  1. 创建标准目录：`my_kb/{raw, processed, wiki, index}` 和源码目录 `src/`。
  2. 初始化环境：`pip install litellm pydantic python-frontmatter jieba crawl4ai python-dotenv`。
  3. 配置 `.env`：填入 `DEEPSEEK_API_KEY`, `ZHIPUAI_API_KEY`, `MOONSHOT_API_KEY` 等。
  4. 编写 `src/llm_adapter.py`：基于 `litellm.completion` 封装 `chat()` 和 `chat_json()` 方法，支持通过字符串无缝切换模型。
* **产出**：一个统一的大模型调用入口，一行代码切换 DeepSeek/GLM/Kimi/Ollama。

## Step 1: 输入网关与原料层 (模块 A -> B1)
* **目标**：实现多平台路由抓取，将原始信息 immutable 地落盘到 `raw/`。
* **动作**：
  1. 编写路由分发函数 `fetch_url(url)`：识别 GitHub (走 `gh` CLI)、微信公众号 (走 Jina Reader)、其他网页 (走 `Crawl4AI`)。
  2. 实现 `normalize()`：将抓取内容封装为标准 `RawEntry` (含 id, source_url, original_text)。
  3. 实现 `save_raw()`：将文本存为 `raw/{id}.txt`，元数据存为 `raw/{id}.meta.json`。
* **产出**：输入一个 URL 或文本，自动在 `raw/` 目录生成归档文件。

## Step 1.5: 输入网关通道分层重构 (V2.2)
* **目标**：将单文件 `gateway.py` 重构为通道 (Channel) 插件化架构，支持知乎收藏夹展开、cookie 参数传递、Crawl4AI 正文渲染。
* **动作**：
  1. **创建目录结构** `src/gateway/{__init__,router,base}.py` + `src/gateway/channels/{__init__,github,zhihu,weixin,generic}.py`。
  2. **定义 Channel 协议** (`base.py`)：`name` / `match(url)→bool` / `fetch(url, cookies=None)→str` / `fetch_items(url, cookies=None)→list[dict]|None`。
  3. **实现瘦路由** (`router.py`)：`fetch_url(url, cookies=None)` 遍历 channels 列表，第一个 `match()` 命中的通道负责处理。`generic` 永远排最后兜底。
  4. **迁移现有通道**：将 `gateway.py` 中的 GitHub / 微信 / 通用代码拆到各自 channel 文件，行为完全不变。
  5. **实现知乎通道** (`zhihu.py`)：
     * `match()`: 正则匹配 `zhihu.com` (collection / answer / zhuanlan)。
     * `fetch_items()`: requests 调 `/api/v4/collections/{id}/items` 分页拉取文章列表 `[{url, title}]`，需 cookie。
     * `fetch()`: 单篇 — Crawl4AI 优先 (Playwright + cookie 注入解决 JS 渲染 + 登录态) → 失败回退 requests + bs4 → 再失败回退 answers API。
  6. **实现 cookie 传递链**：`fetch_url(cookies=)` → `channel.fetch(cookies=)` → 通道内 `cookies or _load_cookies_file()`，多级优先 (显式参数 > 通道专属文件 > 全局文件 > 无)。
  7. **更新 raw_store**：`save_link()` 和新增的 `save_collection()` 接受可选 `cookies` 参数，传递到 `fetch_url()`。
  8. **更新 MCP 工具**：`ingest_url` 自动检测展开型 URL (通过 `is_expandable()` / `fetch_items()`)，收藏夹 URL 自动展开为多篇独立 raw 条目。
  9. **安装新依赖**：`pip install beautifulsoup4 lxml markdownify`，更新 `requirements.txt`。
  10. **删除旧 `gateway.py`**，更新 `__init__.py` 导出路径，确保 `from src import gateway` 仍可用。
* **产出**：网关变为可插拔通道架构，知乎收藏夹一行 `ingest_url` 自动展开 156 篇，新增平台只需加文件不改已有代码。
* **验收**：
  * `python test_mcp.py` 全部 7 工具正常。
  * `ingest_url("https://www.zhihu.com/collection/522614669")` (带 cookie) → 156 篇 raw 条目。
  * `ingest_url("https://www.zhihu.com/collection/522614669")` (无 cookie) → 清晰错误提示，不崩溃。
  * `fetch_url("https://github.com/...")` 行为与重构前一致 (回归测试)。

## Step 2: 加工引擎与结构化产出 (模块 B2)
* **目标**：将原料层的纯文本，通过 LLM (JSON Mode) 加工为带 Frontmatter 的标准 Obsidian 笔记。
* **动作**：
  1. 定义 Pydantic Schema：约束 LLM 必须输出 `{title, conclusion, body_markdown, tags}`。
  2. 编写加工 Prompt：要求提炼 2-3 句核心结论，正文保留关键概念。
  3. 实现 `process_note(raw_id)`：读取 raw -> 调用 `llm.chat_json()` -> Pydantic 校验 -> 使用 `python-frontmatter` 拼装为 `.md` -> 写入 `processed/`。
* **产出**：`raw/` 中的长文本被提炼为结构化的 `processed/*.md`，包含 `## 核心结论`。

## Step 3: 确定性关联打分引擎 (核心算法)
* **目标**：替代 LLM 随意生成双链，用"标签+分词"算法建立精准的笔记关联。
* **动作**：
  1. 初始化 `jieba` 分词器，加载停用词表。
  2. 实现 `compute_relations(new_note_path)`：遍历老笔记，标签重合 +3/个，标题分词重合 +1/个，计算总分。
  3. 筛选分数 >= 4 的笔记，实现 `update_related_links()`：双向更新双方 Frontmatter 的 `related` 字段，并在文末追加 `## 相关笔记` 及 `[[]]` 双链。
  4. 将此步作为钩子，挂载到 Step 2 的加工流程末尾自动执行。
* **产出**：新笔记入库时，自动与老笔记建立干净的、确定性的双向关联。

## Step 4: 索引层构建 (模块 B3)
* **目标**：扫描加工层，生成用于快速检索的 JSON 索引文件。
* **动作**：
  1. 实现 `rebuild_index()`：遍历 `processed/` 所有 `.md`。
  2. 使用正则提取 Frontmatter、`## 核心结论` 段落、正文中的 `[[]]` 链接。
  3. 生成四个核心文件：`summaries.json` (摘要库), `tags.json` (标签倒排), `links.json` (正反向双链表), `moc.json` (主题目录)。
* **产出**：系统具备全量检索能力，为问答流和编译流提供数据基础。

## Step 5: 输出引擎 - 问答流 MVP (模块 C1)
* **目标**：跑通第一个业务闭环，验证"查索引->读结论->综合回答"链路。
* **动作**：
  1. 实现 `qa(question)` 方法。
  2. 将 `summaries.json` 全量塞入 Prompt，让 LLM 选出 Top-3 最相关笔记文件名。
  3. 使用文件 I/O 读取这 3 篇笔记的 `## 核心结论` 段落。
  4. 将结论作为 Context，再次调用 LLM 生成最终回答，并附上来源笔记的 `[[]]` 链接。
* **产出**：一个可以通过命令行提问，并基于你的个人知识库精准作答的 CLI 工具。

## Step 6: Wiki 编译层集成 (模块 B4 - 进阶)
* **目标**：引入 Karpathy 理念，将关联簇笔记自动编译为系统性的 Wiki 综述页。
* **动作**：
  1. `pip install llmwiki` 并研究其 Python API 调用方式。
  2. 编写胶水层 `compile_wiki(topic)`：
     * 扫描 `links.json`，找出节点数 >= 3 的关联簇。
     * 将簇内的 `processed/*.md` 复制到临时工作区，调用 `llmwiki` 进行编译（建议用 Kimi 128k 模型）。
     * 后处理：用 Step 3 算出的确定性关联，覆盖 LLM 生成的悬空双链。
     * 将最终综述页保存至 `wiki/` 目录，并更新索引层。
## Step 9: 定时调度器 (Scheduler 模块)
* **目标**：实现每日自动归档知乎收藏夹 / GitHub Trending 等来源，打通"定时触发 → 抓取 → 加工 → 索引 → Wiki"全链路。
* **设计文档**：`docs/scheduler-design.md`
* **动作**：
  1. 创建 `src/scheduler/` 包：`config.py` (声明式任务配置) + `tasks.py` (可复用任务函数) + `daemon.py` (APScheduler 内嵌可选)。
  2. 实现 `tasks.py` 核心任务：`fetch_zhihu_collections()`, `fetch_github_trending()`, `run_pipeline()`, `run_daily()`。
  3. 实现 `daemon.py`：APScheduler 内嵌到 `kb serve`，每天 08:00 自动执行 `run_daily()`。
  4. 实现 `kb schedule` CLI 子命令：列出状态 / 手动触发单任务 / 启用禁用。
  5. 可选：Windows Task Scheduler XML 模板，一行命令注册系统定时任务。
* **产出**：一条 `kb serve` 命令即启动定时归档，知识库每日自动生长。
* **验收**：
  * `kb serve` 启动后，日志显示 "Scheduler: daily_pipeline 已注册 (08:00)"
  * `kb schedule run daily` 手动触发全流程成功
  * 知乎收藏夹自动抓取后，processed/ 内有对应笔记，关联关系正确

### Step 10: 音视频处理 (拓展)

### Step 11: 多学生协作与学习分析 (拓展)

### 云端同步配置 (GitHub, 基础设施)
* **目标**：实现多端同步与版本控制。
* **动作**：
  1. 在 `my_kb/` 执行 `git init` 并关联 GitHub 私有仓库。
  2. 配置 `.gitignore` 忽略 `__pycache__/`, `.env`, `.obsidian/workspace.json`。
  3. （后续在 Obsidian 中配置自动同步，见第三部分）。
* **产出**：系统不仅能存碎片笔记，还能自动将碎片"编译"成结构完整的知识体系综述。
