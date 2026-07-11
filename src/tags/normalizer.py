"""标签归一化管线 — 7 步 Pipeline。

参考: onlelonely/obsidian-curator 的 7 步标签解析管线

Step 1: Normalize     → 去空格/大小写统一
Step 2: Check Blocked → 拒绝通用标签
Step 3: Resolve Alias → 同义词映射 (依赖 ontology)
Step 4: Exact Match   → 是否在规范表中
Step 5: Fuzzy Match   → Levenshtein 相似度匹配
Step 6: [预留] Semantic Match → 嵌入语义匹配 (当前跳过)
Step 7: Return        → 归一化后标签列表
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from src.config import settings
from src.tags import ontology

logger = logging.getLogger(__name__)

# ---------- Step 1: 标准化 ----------
_RE_SPACES = re.compile(r"\s+")


def _normalize(tag: str) -> str:
    """去前后空格、合并内部空白、统一大小写。"""
    t = tag.strip()
    t = _RE_SPACES.sub(" ", t)
    # 去 # 前缀
    if t.startswith("#"):
        t = t[1:]
    # 统一大小写: 中文保持原样，英文首字母大写的保留
    return t


# ---------- Step 2: 阻止通用标签 ----------
def _is_blocked(tag: str) -> bool:
    return tag.strip().lower() in {b.lower() for b in ontology.BLOCKED_TAGS}


# ---------- 主管线 ----------
def normalize_tags(
    tags: list[str],
    canonical_map: Optional[dict[str, str]] = None,
    fuzzy_threshold: float = 0.90,
) -> list[str]:
    """7 步标签归一化管线。

    Args:
        tags: 原始标签列表
        canonical_map: 规范标签映射 (None 则自动加载)
        fuzzy_threshold: Levenshtein 模糊匹配阈值

    Returns:
        归一化后的标签列表 (去重)
    """
    if canonical_map is None:
        canonical_map = ontology.load_ontology()

    result: list[str] = []

    for tag in tags:
        # Step 1: Normalize
        t = _normalize(tag)
        if not t:
            continue

        # Step 2: Check Blocked
        if _is_blocked(t):
            logger.debug("阻止通用标签: %s", t)
            continue

        # Step 3: Resolve Alias (查规范表)
        canon = canonical_map.get(t)
        if canon:
            if canon not in result:
                result.append(canon)
            continue

        # Step 4: Exact Match (反向查: 自身是否 canonical form)
        if t in canonical_map.values():
            if t not in result:
                result.append(t)
            continue

        # Step 5: Fuzzy Match (Levenshtein + Jaccard)
        fuzzy_hit = None
        best_ratio = 0.0
        t_lower = t.lower()
        for cv in set(canonical_map.values()):
            if cv.lower() == t_lower:
                fuzzy_hit = cv
                break
            # 多策略: Levenshtein + Jaccard
            lev = ontology._levenshtein_ratio(t, cv)  # noqa
            jac = ontology._char_jaccard(t, cv)  # noqa
            score = max(lev, jac)
            # 子串包含: 若 t 是 cv 的子串或 cv 是 t 的子串
            sub = (len(cv) >= 2 and cv.lower() in t_lower) or (len(t_lower) >= 2 and t_lower in cv.lower())
            if sub:
                score = max(score, 0.95)
            if score >= fuzzy_threshold and score > best_ratio:
                best_ratio = score
                fuzzy_hit = cv
        if fuzzy_hit:
            if fuzzy_hit not in result:
                result.append(fuzzy_hit)
            logger.debug("模糊匹配: %s → %s (%.2f)", t, fuzzy_hit, best_ratio)
            continue

        # Step 6: [预留] Semantic Match — 当前直接通过
        # Step 7: New Tag (未匹配到任何规范标签)
        if t not in result:
            result.append(t)

    return result


def normalize_all_notes(dry_run: bool = True) -> dict:
    """遍历所有加工层笔记，归一化其 tags 字段。

    Returns:
        {"total": int, "updated": int, "dry_run": bool}
    """
    import frontmatter as fm

    canonical_map = ontology.load_ontology()
    if not canonical_map:
        return {"total": 0, "updated": 0, "dry_run": dry_run, "error": "ontology not built"}

    total = 0
    updated = 0
    for p in settings.PROCESSED_DIR.glob("*.md"):
        total += 1
        try:
            post = fm.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        old_tags = list(post.metadata.get("tags", []) or [])
        if not old_tags:
            continue
        new_tags = normalize_tags(old_tags, canonical_map=canonical_map)
        if new_tags == old_tags:
            continue
        if not dry_run:
            post.metadata["tags"] = new_tags
            post.metadata["updated"] = __import__("datetime").date.today().isoformat()
            p.write_text(fm.dumps(post, sort_keys=False), encoding="utf-8")
        updated += 1

    logger.info("标签归一化: %d/%d 篇更新 (dry_run=%s)", updated, total, dry_run)
    return {"total": total, "updated": updated, "dry_run": dry_run}
