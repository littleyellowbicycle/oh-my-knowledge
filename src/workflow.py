"""工作流 — 一键式自动管线：录入 → 加工 → 索引 → Wiki 编译。"""

from __future__ import annotations

import logging

from src import raw_store, processor, indexer, wiki_compiler, relations
from src.gateway import is_expandable
from src.tags import ontology as _tag_ontology

logger = logging.getLogger(__name__)


def run_pipeline() -> dict:
    """执行标签本体 → 加工 → 关联 → 索引 → Wiki 编译管线。

    Returns:
        {"ontology": dict, "processed": int, "relations": dict, "index": dict, "wiki": int}
    """
    logger.info("=== Step 0/5: build_tag_ontology ===")
    onto_stats = _tag_ontology.build_ontology(min_freq=2, fuzzy_threshold=0.90, dry_run=False)
    logger.info("本体: %d 个规范标签 (合并 %d 个同义)",
                onto_stats["canonical_forms"], onto_stats["auto_merged"])

    logger.info("=== Step 1/5: process_pending ===")
    paths = processor.process_pending()
    logger.info("加工完成: %d 篇", len(paths))

    logger.info("=== Step 2/4: rebuild_all_relations ===")
    rel_stats = relations.rebuild_all_relations()
    logger.info("关联重算: %d 篇更新 / %d 篇总", rel_stats["updated"], rel_stats["total"])

    logger.info("=== Step 3/4: rebuild_index ===")
    stats = indexer.rebuild_index()
    logger.info("索引完成: notes=%d wiki=%d tags=%d",
                stats["notes"], stats["wiki"], stats["tags"])

    logger.info("=== Step 5/5: compile_wiki ===")
    wiki_results = wiki_compiler.compile_all_wiki()
    logger.info("Wiki 编译完成: %d 篇", len(wiki_results))

    return {
        "ontology": onto_stats,
        "processed": len(paths),
        "relations": rel_stats,
        "index": stats,
        "wiki": len(wiki_results),
    }


def ingest_and_process(url: str, cookies: dict | None = None) -> dict:
    """录入 URL → 自动展开收藏夹 → 跑完整管线。

    Returns:
        {"entries": int, "pipeline": dict}
    """
    logger.info("录入: %s", url)
    if is_expandable(url):
        entries = raw_store.save_collection(url, cookies=cookies)
        entry_count = len(entries)
    else:
        raw_store.save_link(url, cookies=cookies)
        entry_count = 1
    logger.info("归档 %d 篇，启动管线...", entry_count)

    pipeline = run_pipeline()
    return {"entries": entry_count, "pipeline": pipeline}
