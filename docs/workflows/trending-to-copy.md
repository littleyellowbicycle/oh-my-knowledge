# Workflow: GitHub Trending → 自媒体文案

## 触发条件

每日定时任务已自动完成：
1. `fetch_github_trending` — 爬取 Trending 并归档到 `raw/`
2. `run_pipeline` — 加工为结构化笔记并建立索引

此时 `my_kb/processed/` 中已有当天的 Trending 项目笔记。

---

## Step 1: 浏览 Trending 项目

调用 `list_processed`（不指定参数）查看所有已加工笔记，从中挑选值得写的选题。

筛选标准：
- **today_stars 高** — 热度信号
- **概念新颖** — 有传播价值
- **有足够材料** — README 内容丰富

---

## Step 2: 竞品调研

对选中的选题，搜索社交平台上的同类内容：

### 2.1 搜索

用自己的 web search 能力搜索以下平台：

```
site:xiaohongshu.com {topic}
site:bilibili.com {topic}
site:zhihu.com {topic}
site:juejin.cn {topic}
site:mp.weixin.qq.com {topic}
```

### 2.2 归档

对找到的每篇有价值内容，调用 MCP 工具的 `ingest_url(url)` 归档到知识库。
然后调用 `process_pending()` 加工为结构化笔记。

如果搜索结果中有无法直接抓取的页面（如小红书需要登录），**你（agent）自己阅读并总结内容**，然后调用 `ingest_text(text)` 手动录入。

### 2.3 分析差距

调用 `list_processed(tag="{topic}")` 查看已归档的竞品笔记。
阅读笔记，找出：
- 竞品覆盖了什么角度
- **什么角度没人写**（这就是你的差异化机会）

---

## Step 3: 生成文案

### 方式 A：用项目的 LLM

调用 `generate_copy(topic, context)`，把 Step 2 的分析结果作为 `context` 传入。

### 方式 B：你自己写

你（agent）直接生成文案，然后调用 `ingest_text(text)` 归档。

文案结构（AIDA）：
1. **标题** — 有数字/冲突/悬念，15-25 字
2. **开头 Hook** — 1-3 句抓住注意力
3. **正文** — 有案例、数据、代码片段（如有）
4. **结尾引导** — 总结 + 互动/关注引导

输出保存到 `my_kb/output/copy_{topic}.md`。

---

## 后续流程（待添加）

各流程为独立文档，按需创建：

| 流程 | 说明 |
|------|------|
| `trending-to-image.md` | 文案 → 配图生成（Playwright 渲染 / AI 绘图） |
| `trending-to-video.md` | 文案 → 视频（TTS + FFmpeg + 动效） |
| `trending-to-publish.md` | 发布到小红书/公众号/B站（DrissionPage 自动化） |

---

## 竞品账号管理

不固定为代码配置文件。**由你（agent）在执行调研时自己决定搜索策略**：每次搜索 `site:{platform} {topic}` 即可覆盖相关内容。

如果你发现某个账号多次出现（常驻竞品），可以记录在当前对话中，下次调研时直接检查该账号的最新内容。
