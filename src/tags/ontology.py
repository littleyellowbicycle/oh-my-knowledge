"""标签本体管理 — 从存量笔记中构建规范标签表。

职责:
    1. 扫描加工层笔记，统计标签频次
    2. 用 Levenshtein 模糊匹配 + 出现频次自动聚类
    3. 生成规范标签表 CANONICAL_MAP
    4. 暴露 get_canonical() / get_high_frequency_tags() / load_ontology()

设计原则:
    - 频次最高者自动当选 canonical form
    - 保留人工裁决接口: 用户可编辑 CANONICAL_MAP 覆盖自动选择
    - 全量重建幂等: 重复运行不产生新变更
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections import Counter
from pathlib import Path
from typing import Optional

from src.config import settings

logger = logging.getLogger(__name__)

ONTOLOGY_FILE = settings.INDEX_DIR / "tag_ontology.json"

# ---------- 种子规范标签映射 (可编辑) ----------
# 格式: {variant: canonical}
# 用户可手动补充不在此表中的映射
SEED_CANONICAL_MAP: dict[str, str] = {
    # A 股
    "A股市场": "A股",
    "A股投资": "A股",
    "A股入门": "A股",
    "A股操盘": "A股",
    "A股市场分析": "A股",
    # 投资
    "个人投资": "投资",
    "投资分析": "投资",
    # 散户
    "散户策略": "散户投资",
    "散户心理": "散户投资",
    # 风险
    "风险管理": "风险控制",
    "风控": "风险控制",
    # 技术分析
    "K线": "技术分析",
    "K线图": "技术分析",
    "量价分析": "技术分析",
    "量价关系": "技术分析",
    # 量化
    "量化投资": "量化交易",
    # 交易
    "交易策略": "投资策略",
    # 基金
    "基金投资": "基金",
    "公募基金": "基金",
    "基金经理": "基金",
    # 指数
    "指数投资": "指数基金",
    # 均线
    "均线系统": "均线",
    # 选股
    "选股策略": "选股",
    # 趋势
    "趋势跟踪": "趋势交易",
    # 复利
    "长期投资": "长线投资",
    # 宏观
    "宏观经济": "宏观",
    # 大盘
    "大盘分析": "大盘",
    # 牛市
    "牛市策略": "牛市",
    "牛市见顶": "牛市",
    # 因子
    "因子模型": "量化因子",
    # 估值
    "估值分析": "估值",
    # 财务
    "财务分析": "基本面分析",
    # 情绪
    "情绪周期": "市场情绪",
    # 区块链/币
    "区块链技术": "区块链",
    "加密货币": "区块链",
    # 知识付费
    "付费课程": "知识付费",
    "课程评测": "知识付费",
    # 教育
    "投资教育": "投资学习",
    "金融交易": "金融",
}

# 被阻止的通用标签 (出现则丢弃)
BLOCKED_TAGS: set[str] = {
    "笔记", "总结", "待办", "todo", "TODO",
    "未分类", "uncategorized", "杂项",
    "转载", "翻译", "原创",
}


def _levenshtein_ratio(a: str, b: str) -> float:
    """计算两个字符串的 Levenshtein 相似度 (0.0 ~ 1.0)。"""
    if not a or not b:
        return 0.0
    a, b = a.lower(), b.lower()
    if a == b:
        return 1.0
    if len(a) < len(b):
        a, b = b, a
    prev = range(len(b) + 1)
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            cost = 0 if ca == cb else 1
            curr.append(min(
                curr[j] + 1,          # 插入
                prev[j + 1] + 1,       # 删除
                prev[j] + cost,        # 替换
            ))
        prev = curr
    return 1.0 - prev[-1] / max(len(a), len(b))


def _collect_all_tags() -> Counter:
    """扫描加工层笔记，返回标签频次 Counter。"""
    counter: Counter = Counter()
    import frontmatter as fm
    for p in settings.PROCESSED_DIR.glob("*.md"):
        try:
            post = fm.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for tag in post.metadata.get("tags", []):
            t = tag.strip()
            if t:
                counter[t] += 1
    return counter



def _char_jaccard(a: str, b: str) -> float:
    """字符级 Jaccard 相似度，适合中文标签比较。"""
    set_a, set_b = set(a), set(b)
    if not set_a or not set_b:
        return 0.0
    inter = len(set_a & set_b)
    union = len(set_a | set_b)
    return inter / union if union else 0.0


def _contains_variant(tag: str, candidates: list[tuple[str, int]]) -> Optional[str]:
    """检查 tag 是否包含某个高频候选标签的子串。"""
    tl = tag.lower()
    for ct, freq in candidates:
        if len(ct) >= 2 and ct.lower() in tl and ct != tag:
            return ct
    return None


def build_ontology(
    min_freq: int = 2,
    fuzzy_threshold: float = 0.85,
    dry_run: bool = True,
) -> dict:
    """扫描加工层笔记，构建规范标签映射并落盘。

    流程:
        1. 统计所有标签频次
        2. 应用 SEED_CANONICAL_MAP 种子映射
        3. 对剩余标签做多策略同义检测: Levenshtein / Jaccard / 子串包含
        4. 合并种子 + 自动发现的映射，写入 tag_ontology.json

    Args:
        min_freq: 最小频次，低于此的标签不参与聚类 (但保留映射)
        fuzzy_threshold: Levenshtein 相似度阈值，>= 此值视为同义
        dry_run: True 只报告不落盘

    Returns:
        {"total_tags": int, "canonical_forms": int, "merged": int,
         "suggested": [(variant, canonical, reason)], ...}
    """
    counter = _collect_all_tags()
    total_tags = len(counter)

    # 1. 种子映射
    canonical_map: dict[str, str] = {}
    merged = set()
    for variant, canon in SEED_CANONICAL_MAP.items():
        if variant in counter:
            canonical_map[variant] = canon
            merged.add(variant)
        elif variant in counter or canon in counter:
            canonical_map[variant] = canon
            merged.add(variant)

    # 2. 按频次降序排序，高频优先作为 canonical
    sorted_tags = [t for t, _ in counter.most_common() if t not in merged]
    auto_suggestions: list[tuple[str, str, str]] = []

    # 2a. 子串包含匹配: 低频标签包含高频标签名 (如 A股市场 → A股)
    high_freq_candidates: list[tuple[str, int]] = [
        (t, c) for t, c in counter.most_common() if c >= min_freq
    ]
    for tag in sorted_tags:
        if tag in merged:
            continue
        parent = _contains_variant(tag, high_freq_candidates)
        if parent and counter[parent] >= counter[tag]:
            canonical_map[tag] = parent
            auto_suggestions.append((tag, parent, f"子串包含: {parent} ⊆ {tag}"))
            merged.add(tag)

    # 2b. 多策略模糊匹配: Levenshtein + Jaccard 字符级相似度
    i = 0
    while i < len(sorted_tags):
        tag = sorted_tags[i]
        if tag in merged or tag in canonical_map.values():
            i += 1
            continue
        best_canon: Optional[str] = None
        best_score = 0.0
        for j in range(i):
            other = sorted_tags[j]
            if counter[other] < counter[tag]:
                continue
            # 三策略取最高分
            lev = _levenshtein_ratio(tag, other)
            jac = _char_jaccard(tag, other)
            score = max(lev, jac)
            if score >= fuzzy_threshold and score > best_score:
                best_canon = other
                best_score = score
        if best_canon:
            canonical_map[tag] = best_canon
            auto_suggestions.append((tag, best_canon, f"模糊匹配 ({best_score:.2f})"))
            merged.add(tag)
        i += 1

    # 3. 未被映射的标签作为 canonical form 自身
    for tag in counter:
        if tag not in merged and tag not in canonical_map.values():
            canonical_map[tag] = tag

    result = {
        "total_tags": total_tags,
        "canonical_forms": len(set(canonical_map.values())),
        "seed_merged": len([v for v in merged if v in SEED_CANONICAL_MAP]),
        "auto_merged": len(auto_suggestions),
        "frequency": {tag: count for tag, count in counter.most_common(20)},
        "suggestions": auto_suggestions,
    }

    if not dry_run:
        _write_ontology(canonical_map)
        logger.info("本体已写入: %s (%d 个规范标签)", ONTOLOGY_FILE, result["canonical_forms"])
    else:
        logger.info("dry-run: 发现 %d 个规范标签 (可合并 %d 个同义标签)",
                     result["canonical_forms"], result["auto_merged"])
        for variant, canon, reason in auto_suggestions:
            logger.info("  建议: %s → %s (%s)", variant, canon, reason)

    return result


# ---------- 持久化 ----------
def _write_ontology(canonical_map: dict[str, str]) -> None:
    """落盘规范标签映射到索引层。"""
    settings.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    data = {
        "version": "1.0",
        "canonical_map": canonical_map,
    }
    ONTOLOGY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def load_ontology() -> dict[str, str]:
    """加载规范标签映射，返回 {variant: canonical}。

    若不存在则返回空 dict (不自动重建)。
    """
    if not ONTOLOGY_FILE.exists():
        return {}
    try:
        data = json.loads(ONTOLOGY_FILE.read_text(encoding="utf-8"))
        return data.get("canonical_map", {})
    except Exception as e:  # noqa: BLE001
        logger.warning("加载本体失败: %s", e)
        return {}


def get_canonical(tag: str, canonical_map: Optional[dict[str, str]] = None) -> str:
    """返回 tag 的规范形式。若无映射则原样返回。"""
    if canonical_map is None:
        canonical_map = load_ontology()
    return canonical_map.get(tag, tag)


def get_high_frequency_tags(min_freq: int = 3) -> list[str]:
    """返回出现 >= min_freq 次的标签列表 (按频次降序)。

    用于 Prompt 约束: 告诉 LLM 优先复用哪些标签。
    """
    counter = _collect_all_tags()
    return [tag for tag, count in counter.most_common() if count >= min_freq]
