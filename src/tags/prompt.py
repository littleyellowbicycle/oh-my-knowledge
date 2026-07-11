"""Prompt 约束块生成 — 方案A: 在 LLM 加工 Prompt 中插入标签约束。

让 LLM 优先复用知识库中的高频标签，同时允许在必要时创建新标签。
"""

from __future__ import annotations

from src.tags.ontology import get_high_frequency_tags


def tag_constraint_block(max_reuse: int = 4, max_new: int = 3) -> str:
    """生成插入到加工 Prompt 中的标签约束段落。

    内容:
        1. 当前知识库高频标签列表 (出现 >= 3 次)
        2. 优先级: 优先从已有标签中选择
        3. 上限: 最多 max_reuse 个已有标签 + max_new 个新标签

    Returns:
        格式化的约束文本 (空行分隔，可直接追加到 Prompt 中)
    """
    high_freq = get_high_frequency_tags(min_freq=3)

    lines = [
        "【标签要求】",
        f"- 优先复用以下已有标签 (最多选 {max_reuse} 个):",
    ]
    # 每行 5 个标签
    batch = []
    for tag in high_freq[:30]:  # 只传前 30 个高频标签，避免 Prompt 过长
        batch.append(tag)
        if len(batch) == 5:
            lines.append("  " + "、".join(batch))
            batch = []
    if batch:
        lines.append("  " + "、".join(batch))

    lines += [
        f"- 若以上标签不足以覆盖内容主题，可创建新标签 (最多 {max_new} 个)",
        "- 新标签需简洁 (1-3 个词)、具体、非通用",
        "- 标签总数控制在 3-6 个",
    ]
    return "\n".join(lines)
