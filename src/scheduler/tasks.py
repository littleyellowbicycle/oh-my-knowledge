"""可复用任务函数 — 调度器和 CLI 手动触发共用同一套逻辑。

每个任务返回 TaskResult，调用方据此汇总日志或通知。
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import re
import time
from dataclasses import dataclass, field

import requests
from bs4 import BeautifulSoup

from src import gateway, raw_store, workflow
from src.scheduler import config

logger = logging.getLogger(__name__)


# ---------- 结果模型 ----------
@dataclass
class TaskResult:
    name: str
    success: bool
    entries: int = 0          # 新增 raw 条目数
    duration: float = 0.0     # 耗时秒数
    error: str | None = None
    detail: list[str] = field(default_factory=list)


# ---------- 单任务 ----------
def fetch_zhihu_collections() -> TaskResult:
    """遍历 config.ZHIHU_COLLECTIONS，逐篇抓取落盘 raw/。"""
    start = time.time()
    urls = config.ZHIHU_COLLECTIONS
    name = "fetch_zhihu_collections"
    if not urls:
        logger.info("[%s] 未配置知乎收藏夹，跳过", name)
        return TaskResult(name=name, success=True, entries=0,
                          duration=time.time() - start,
                          detail=["未配置收藏夹 URL"])

    total = 0
    detail: list[str] = []
    for url in urls:
        try:
            from src.gateway import is_expandable
            if is_expandable(url):
                entries = raw_store.save_collection(url)
                total += len(entries)
                detail.append(f"{url} → {len(entries)} 篇")
            else:
                raw_store.save_link(url)
                total += 1
                detail.append(f"{url} → 1 篇")
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 抓取失败 %s: %s", name, url, e)
            detail.append(f"{url} → 失败: {e}")

    return TaskResult(name=name, success=True, entries=total,
                      duration=time.time() - start, detail=detail)


def fetch_github_trending() -> TaskResult:
    """爬 GitHub Trending → 逐仓库落盘 raw/。"""
    start = time.time()
    name = "fetch_github_trending"
    total = 0
    detail: list[str] = []

    for lang in config.TRENDING_LANGUAGES:
        try:
            repos = _scrape_trending(lang, config.TRENDING_SINCE)
            count = 0
            for repo in repos:
                readme = _fetch_readme(repo["url"])
                text = _format_trending_repo(repo, readme)
                raw_store.save_manual(text, source_url=repo["url"])
                count += 1
                total += 1
            detail.append(f"{lang}: {count} 个仓库")
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] 抓取 %s 失败: %s", name, lang, e)
            detail.append(f"{lang}: 失败 - {e}")

    return TaskResult(name=name, success=True, entries=total,
                      duration=time.time() - start, detail=detail)


def fetch_url_list(urls: list[str]) -> TaskResult:
    """通用 URL 列表批量抓取落盘。"""
    start = time.time()
    name = "fetch_url_list"
    total = 0
    detail: list[str] = []
    for url in urls:
        try:
            raw_store.save_link(url)
            total += 1
        except Exception as e:  # noqa: BLE001
            logger.warning("[%s] %s 失败: %s", name, url, e)
            detail.append(f"{url}: {e}")
    return TaskResult(name=name, success=True, entries=total,
                      duration=time.time() - start, detail=detail)


def run_pipeline() -> TaskResult:
    """加工 pending → 重建索引 → 编译 Wiki。"""
    start = time.time()
    name = "run_pipeline"
    try:
        result = workflow.run_pipeline()
        detail = [
            f"本体: {result['ontology']['canonical_forms']} 规范标签",
            f"加工: {result['processed']} 篇",
            f"关联: {result['relations']['updated']}/{result['relations']['total']} 更新",
            f"索引: {result['index']['notes']} 笔记",
            f"Wiki: {result['wiki']} 篇",
        ]
        return TaskResult(name=name, success=True, entries=result["processed"],
                          duration=time.time() - start, detail=detail)
    except Exception as e:  # noqa: BLE001
        return TaskResult(name=name, success=False,
                          duration=time.time() - start, error=str(e))


# ---------- 组合任务 ----------
def run_daily() -> list[TaskResult]:
    """每日全流程: 知乎收藏夹 + GitHub Trending + 管线加工。

    返回各子任务结果列表，供调用方汇总日志/通知。
    """
    results: list[TaskResult] = []
    logger.info("=== 每日定时任务开始 %s ===", _dt.datetime.now().strftime("%Y-%m-%d %H:%M"))

    results.append(fetch_zhihu_collections())
    results.append(fetch_github_trending())
    results.append(run_pipeline())

    total_entries = sum(r.entries for r in results)
    total_time = sum(r.duration for r in results)
    logger.info("=== 每日定时任务完成: %d 篇入库, 耗时 %.1fs ===",
                total_entries, total_time)
    return results


# ---------- GitHub Trending 爬虫 ----------
def _scrape_trending(language: str, since: str = "daily") -> list[dict]:
    """爬取 github.com/trending 返回仓库列表。

    language="any" 时不指定语言，抓取所有语言的热榜。
    返回: [{name, url, description, stars, today_stars, language}]
    """
    path = f"/trending/{language}" if language != "any" else "/trending"
    url = f"https://github.com{path}?since={since}"
    resp = requests.get(url, timeout=config.FETCH_TIMEOUT, headers={
        "User-Agent": "Mozilla/5.0 (my-knowledge-base scheduler)"
    })
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "lxml")

    repos: list[dict] = []
    for article in soup.select("article"):
        h2 = article.select_one("h2 a")
        if not h2:
            continue
        repo_path = h2.get("href", "").strip().lstrip("/")
        desc_el = article.select_one("p")
        stars_el = article.select_one("a[href*='stargazers']")
        today_el = article.select_one("span.d-inline-block")

        repos.append({
            "name": repo_path,
            "url": f"https://github.com/{repo_path}",
            "description": (desc_el.get_text(strip=True) if desc_el else ""),
            "stars": (stars_el.get_text(strip=True) if stars_el else ""),
            "today_stars": (today_el.get_text(strip=True) if today_el else ""),
            "language": language,
        })
    return repos


def _fetch_readme(repo_url: str, max_chars: int = 30000) -> str:
    """从 GitHub API 拉取仓库 README（原始 markdown）。

    支持 GITHUB_TOKEN 环境变量提高 API 速率限制（60 → 5000 次/小时）。
    """
    match = re.match(r"https://github\.com/([^/]+/[^/]+)", repo_url)
    if not match:
        return ""
    repo_path = match.group(1)
    try:
        headers = {
            "Accept": "application/vnd.github.v3.raw",
            "User-Agent": "my-knowledge-base/1.0",
        }
        token = os.getenv("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        resp = requests.get(
            f"https://api.github.com/repos/{repo_path}/readme",
            headers=headers,
            timeout=15,
        )
        if resp.ok:
            text = resp.text
            if len(text) > max_chars:
                text = text[:max_chars] + "\n\n[... README 过长，已截断 ...]"
            return text
        logger.warning("获取 README 失败 %s: HTTP %d", repo_path, resp.status_code)
    except Exception as e:
        logger.warning("获取 README 异常 %s: %s", repo_path, e)
    return ""


def _format_trending_repo(repo: dict, readme: str = "") -> str:
    """将单个 trending 仓库格式化为 raw markdown。

    如果提供了 readme 正文，追加到元信息之后，供 processor 参考。
    """
    parts = [
        f"# GitHub Trending: {repo['name']}\n",
        f"- URL: {repo['url']}\n",
        f"- Stars: {repo['stars']}\n",
        f"- Today: {repo['today_stars']}\n",
        f"- Language: {repo['language']}\n",
        f"- Description: {repo['description']}\n",
    ]
    if readme:
        parts.append(f"\n--- README 原文 ---\n{readme}\n")
    return "\n".join(parts)
