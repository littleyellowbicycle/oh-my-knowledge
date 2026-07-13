"""原料层 (模块 B1) - 只读归档存储。

职责 (严格遵循架构 V2.1 原料层规则):
    * normalize_and_save()  把任意输入归一化为 RawEntry 并落盘
    * 文件名: raw_{timestamp}_{short_hash}.md  (抓取/manual 输出均为 markdown)
    * 元数据: 同名 .meta.json (不含原文)
    * 不可变: 写入后只读，本模块不提供任何修改/删除接口
    * 保留原始格式: 不做任何清洗

对外暴露:
    save_manual(text)              -> RawEntry   手动输入落盘
    save_link(url, text=None)      -> RawEntry   链接抓取落盘 (text 已抓则直接用)
    save_collection(url)           -> list[RawEntry]  收藏夹 URL -> 多篇落盘
    load_raw(raw_id)               -> RawEntry   读取原料条目 (含原文)
    mark_status(raw_id, status)    -> None       推进状态 (pending->processed->indexed)
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import json
import logging
from pathlib import Path

from src.config import settings
from src.schemas import RawEntry, RawStatus, SourceType
from src import gateway

logger = logging.getLogger(__name__)


# ---------- ID 与文件名生成 ----------
def _now_iso() -> str:
    return _dt.datetime.now().strftime("%Y-%m-%dT%H:%M:%S")


def _gen_raw_id(text: str) -> str:
    """生成 raw_{YYYYMMDD_HHMMSS}_{8位hash} 形式的 ID。"""
    ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    short_hash = hashlib.md5(text.encode("utf-8")).hexdigest()[:8]
    return f"raw_{ts}_{short_hash}"


def _raw_path(raw_id: str, ext: str = "md") -> Path:
    return settings.RAW_DIR / f"{raw_id}.{ext}"


def _meta_path(raw_id: str) -> Path:
    return settings.RAW_DIR / f"{raw_id}.meta.json"


# ---------- 归一化与落盘核心 ----------
def _save(entry: RawEntry) -> RawEntry:
    """内部落盘: 写原文 + 写 meta.json，返回 entry。"""
    settings.ensure_dirs()
    content_path = _raw_path(entry.id, "md")
    meta_path = _meta_path(entry.id)

    if content_path.exists():
        # 不可变: 不允许覆盖
        raise FileExistsError(f"原料文件已存在 (不可变): {content_path}")

    # 1) 原文
    content_path.write_text(entry.original_text, encoding="utf-8")
    # 2) 元数据 (不含原文)
    meta_path.write_text(
        json.dumps(entry.to_meta(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    logger.info("原料落盘: %s (type=%s, %d 字符)",
                entry.id, entry.source_type, len(entry.original_text))
    return entry


def save_manual(text: str) -> RawEntry:
    """手动输入 -> 归一化 -> 落盘。"""
    text = (text or "").strip()
    if not text:
        raise ValueError("手动输入内容不能为空")
    entry = RawEntry(
        id=_gen_raw_id(text),
        source_type=SourceType.MANUAL,
        source_url=None,
        original_text=text,
        ingested_at=_now_iso(),
        status=RawStatus.PENDING,
    )
    return _save(entry)


def save_link(url: str, text: str | None = None, cookies: dict | None = None) -> RawEntry:
    """链接抓取 -> 归一化 -> 落盘。

    Args:
        url:     目标链接
        text:    可选，已抓取的文本；None 则自动调用 gateway.fetch_url(url)
        cookies: 可选登录态 (传递给网关通道，如知乎 cookie)
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("URL 不能为空")
    if text is None:
        text = gateway.fetch_url(url, cookies=cookies)
    if not text or not text.strip():
        raise RuntimeError(f"抓取内容为空: {url}")
    entry = RawEntry(
        id=_gen_raw_id(text),
        source_type=SourceType.LINK,
        source_url=url,
        original_text=text,
        ingested_at=_now_iso(),
        status=RawStatus.PENDING,
    )
    return _save(entry)


def save_collection(url: str, cookies: dict | None = None) -> list[RawEntry]:
    """展开型 URL (如知乎收藏夹) -> 获取文章列表 -> 逐篇 save_link 落盘。

    每篇文章存为独立的 raw 条目，便于后续逐篇加工。

    Args:
        url:     收藏夹链接 (如 https://www.zhihu.com/collection/123)
        cookies: 可选登录态

    Returns:
        成功落盘的 RawEntry 列表 (跳过失败的文章)
    """
    url = (url or "").strip()
    if not url:
        raise ValueError("URL 不能为空")

    items = gateway.fetch_items(url, cookies=cookies)
    if not items:
        raise RuntimeError(f"未获取到任何文章: {url}")

    entries: list[RawEntry] = []
    failed: list[str] = []
    for i, item in enumerate(items, 1):
        try:
            logger.info("收藏夹进度: %d/%d %s", i, len(items), item["title"][:40])
            entry = save_link(item["url"], cookies=cookies)
            entries.append(entry)
        except Exception as e:  # noqa: BLE001
            logger.warning("跳过文章 [%d] %s: %s", i, item.get("url", ""), e)
            failed.append(item.get("url", ""))

    if failed:
        logger.warning("收藏夹 %s: 成功 %d 篇, 失败 %d 篇", url, len(entries), len(failed))
    return entries


# ---------- 读取与状态推进 ----------
def load_raw(raw_id: str) -> RawEntry:
    """读取原料条目 (含原文)。"""
    meta_path = _meta_path(raw_id)
    content_path = _raw_path(raw_id, "md")
    if not meta_path.exists():
        raise FileNotFoundError(f"原料不存在: {raw_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["original_text"] = content_path.read_text(encoding="utf-8")
    return RawEntry.model_validate(meta)


def list_raw(status: RawStatus | None = None) -> list[str]:
    """列出所有原料 ID (按 ingested_at 升序)。可选按状态过滤。"""
    metas: list[tuple[str, str]] = []
    for p in settings.RAW_DIR.glob("*.meta.json"):
        try:
            m = json.loads(p.read_text(encoding="utf-8"))
            if status and m.get("status") != status.value:
                continue
            metas.append((m.get("ingested_at", ""), m["id"]))
        except Exception as e:  # noqa: BLE001
            logger.warning("跳过损坏的 meta: %s (%s)", p, e)
    metas.sort()
    return [mid for _, mid in metas]


def mark_status(raw_id: str, status: RawStatus) -> None:
    """推进原料状态 (pending -> processed -> indexed)。
    """
    meta_path = _meta_path(raw_id)
    if not meta_path.exists():
        raise FileNotFoundError(f"原料不存在: {raw_id}")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["status"] = status.value
    meta_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def iter_pending() -> list[str]:
    """便利方法: 列出所有 pending 状态的原料 ID。"""
    return list_raw(status=RawStatus.PENDING)
