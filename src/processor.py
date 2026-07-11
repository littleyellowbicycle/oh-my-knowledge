"""加工引擎 (模块 B2) - LLM JSON Mode 结构化产出。

职责:
    将原料层纯文本通过 LLM 加工为带 Frontmatter 的标准 Obsidian 笔记，
    落盘到 processed/ 目录。
    集成标签约束 (方案A) + 标签归一化 (方案B)。

流程:
    1. 读取 raw 原文
    2. 构造加工 Prompt (含标签约束，要求优先复用高频标签)
    3. 调用 llm.chat_json(schema=ProcessedNote)
    4. 归一化 LLM 输出的 tags (同义词/模糊匹配折叠)
    5. 用 python-frontmatter 拼装 .md
    6. 写入 processed/
    7. 标记原料状态为 processed
    8. 触发关联引擎钩子
"""

from __future__ import annotations

import datetime as _dt
import logging
import re
from pathlib import Path
from typing import Callable, Optional

import frontmatter

from src.config import settings
from src.llm_adapter import llm, LLMError
from src.raw_store import load_raw, mark_status, iter_pending
from src.schemas import NoteType, ProcessedNote, RawStatus
from src import relations as _relations
from src.tags import normalizer as _tag_normalizer
from src.tags import prompt as _tag_prompt

from src.gateway.channels._shared import detect_raw_type

logger = logging.getLogger(__name__)

# 加工完成的笔记默认状态字符串
NOTE_STATUS_PROCESSED = "processed"

# 关联引擎钩子签名: on_processed(note_path: Path) -> None
OnProcessedHook = Callable[[Path], None]


# ---------- Prompt 构造 ----------
_PROCESS_SYSTEM = (
    "你是一名知识管理编辑。把用户提供的原始材料加工成一篇结构化 Obsidian 笔记。"
    "要求: 1) 核心结论 2-3 句话，直击要害；"
    "2) 正文用中文重写，逻辑清晰，必须在关键概念处使用 [[双链]] 标记；"
    "3) 标签 3-6 个，覆盖主题与领域，优先复用已有标签；"
    "4) title 简洁有信息量，不带书名号/引号。"
)


def _build_prompt(raw_text: str, source_url: Optional[str]) -> str:
    src_line = f"\n原始来源链接: {source_url}" if source_url else ""
    return (
        "请把以下原始材料加工为一篇结构化笔记。严格按 JSON Schema 输出。\n"
        f"{src_line}\n\n"
        "===== 原始材料 =====\n"
        f"{raw_text}\n"
        "===== 原始材料结束 =====\n\n"
        f"{_tag_prompt.tag_constraint_block()}\n"
    )


# ---------- 文件名安全化 ----------
_INVALID_FS_CHARS = re.compile(r'[\\/:*?"<>|\n\r\t]')


def _safe_filename(title: str) -> str:
    """把标题转为安全的文件名 (保留中文，去非法字符)。"""
    name = _INVALID_FS_CHARS.sub(" ", title).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        name = "未命名笔记"
    # Windows 文件名长度上限留余量
    return name[:80]


def _unique_path(title: str) -> Path:
    """在 processed/ 下生成不冲突的文件路径。"""
    base = _safe_filename(title)
    candidate = settings.PROCESSED_DIR / f"{base}.md"
    i = 2
    while candidate.exists():
        candidate = settings.PROCESSED_DIR / f"{base} ({i}).md"
        i += 1
    return candidate


# ---------- Markdown 拼装 ----------
def _assemble_markdown(note: ProcessedNote, raw_id: str,
                       source_url: Optional[str], today: str) -> str:
    """按架构 V2.1 标准结构拼装最终 .md 文本。"""
    post = frontmatter.Post(
        content=_body_with_sections(note),
        title=note.title,
        source=raw_id,
        source_url=source_url or "",
        created=today,
        updated=today,
        tags=note.tags,
        status=NOTE_STATUS_PROCESSED,
        note_type=note.note_type.value,
        related=note.related,
    )
    return frontmatter.dumps(post, sort_keys=False)


def _make_stub_note(entry) -> ProcessedNote:
    """抓取失败的原料 → 占位笔记 (不调 LLM)。"""
    url = entry.source_url or "(未知来源)"
    return ProcessedNote(
        title=f"待补全 - {entry.id[-12:]}",
        conclusion=f"原始来源未获取，待补全。来源: {url}",
        body_markdown=(
            f"⚠️ **待补全** — 原始内容抓取失败。\n\n"
            f"## 状态\n\n本笔记为占位条目，无实质论点。\n\n"
            f"## 来源\n\n{url}\n\n"
            f"## 补全方式\n\n"
            f"1. 检查 cookies 配置 (如需登录态)\n"
            f"2. 重置原料状态为 pending 后重新加工"
        ),
        tags=["待补全", "抓取失败"],
        related=[],
        note_type=NoteType.STUB,
    )


def _make_index_note(entry) -> ProcessedNote:
    """收藏夹索引页 → 索引笔记 (保留链接列表，不调 LLM)。"""
    url = entry.source_url or ""
    text = entry.original_text
    link_count = sum(1 for line in text.split("\n") if "](" in line)
    return ProcessedNote(
        title=f"收藏夹索引 - {entry.id[-12:]}",
        conclusion=f"收藏夹索引页，包含 {link_count} 篇文章链接。",
        body_markdown=(
            f"本笔记为收藏夹目录页，仅含链接列表。\n\n"
            f"## 来源\n\n{url}\n\n"
            f"## 文章列表\n\n{text}\n"
        ),
        tags=["索引页", "收藏夹"],
        related=[],
        note_type=NoteType.INDEX,
    )


def _body_with_sections(note: ProcessedNote) -> str:
    """拼装正文: 标题 + 核心结论 + 详细内容 + 相关笔记(占位，关联引擎覆写)。"""
    conclusion = note.conclusion.strip()
    if not conclusion.startswith(">"):
        conclusion = "> " + conclusion.replace("\n", "\n> ")
    related_lines = "\n".join(f"- [[{r}]]" for r in note.related) or "- (待关联引擎填充)"
    return (
        f"# {note.title}\n\n"
        f"## 核心结论\n{conclusion}\n\n"
        f"## 详细内容\n{note.body_markdown.strip()}\n\n"
        f"## 相关笔记\n{related_lines}\n"
    )


# ---------- 主流程 ----------
def process_note(
    raw_id: str,
    *,
    on_processed: Optional[OnProcessedHook] = None,
    model: Optional[str] = None,
) -> Path:
    """加工单篇原料为 Obsidian 笔记，返回笔记路径。

    Args:
        raw_id: 原料层 ID
        on_processed: 加工完成后的钩子；None 则默认调用关联引擎 compute_and_apply
        model: 指定 LLM 模型；None 用 settings.MODEL_PROCESS
    """
    entry = load_raw(raw_id)
    if entry.original_text.strip() == "":
        raise ValueError(f"原料原文为空: {raw_id}")
    if entry.status == RawStatus.PROCESSED:
        raise ValueError(
            f"原料 {raw_id} 已加工，重复加工会产生重复笔记。"
            f"如需重新加工请先删除对应 processed 笔记并重置原料状态为 pending"
        )

    # 检测原料类型，stub/index 不调 LLM
    raw_type = detect_raw_type(entry.original_text)
    if raw_type == "stub":
        note = _make_stub_note(entry)
        logger.info("加工 %s -> stub (跳过 LLM)", raw_id)
    elif raw_type == "index":
        note = _make_index_note(entry)
        logger.info("加工 %s -> index (跳过 LLM)", raw_id)
    else:
        prompt = _build_prompt(entry.original_text, entry.source_url)
        logger.info("开始加工 %s (模型=%s)", raw_id, model or settings.MODEL_PROCESS)
        note = llm.chat_json(
            prompt,
            schema=ProcessedNote,
            system=_PROCESS_SYSTEM,
            model=model or settings.MODEL_PROCESS,
        )
        note.note_type = NoteType.CONTENT
        # 方案B: 标签归一化 (同义词/模糊匹配折叠)
        note.tags = _tag_normalizer.normalize_tags(note.tags)

    today = _dt.date.today().isoformat()
    content = _assemble_markdown(note, raw_id, entry.source_url, today)

    out_path = _unique_path(note.title)
    settings.ensure_dirs()
    out_path.write_text(content, encoding="utf-8")
    logger.info("加工完成 -> %s", out_path.name)

    # 触发关联引擎钩子 (默认调用 Step 3 的 compute_and_apply)
    # 先运行钩子，成功后推进原料状态，避免原料标记 processed 但关联缺失
    hook = on_processed if on_processed is not None else _relations.compute_and_apply
    try:
        hook(out_path)
    except Exception as e:  # noqa: BLE001
        logger.warning("on_processed 钩子失败: %s", e)
        # 清理孤儿笔记，避免重试时产生重复文件并污染关联打分
        out_path.unlink(missing_ok=True)
        # 清理老笔记中可能已写入的悬空双链 (关联引擎部分成功时的残留)
        try:
            _relations.remove_note_relations(out_path.stem)
        except Exception as cleanup_err:  # noqa: BLE001
            logger.warning("清理悬空关联失败: %s", cleanup_err)
        # 原料保持 pending，用户可下次重新加工
        mark_status(raw_id, RawStatus.PENDING)
        raise LLMError(f"加工钩子失败，已回滚: {e}") from e

    mark_status(raw_id, RawStatus.PROCESSED)
    return out_path


def process_pending(
    *,
    on_processed: Optional[OnProcessedHook] = None,
    model: Optional[str] = None,
    limit: Optional[int] = None,
) -> list[Path]:
    """批量加工所有 pending 状态的原料，返回成功生成的笔记路径列表。"""
    ids = iter_pending()
    if limit:
        ids = ids[:limit]
    results: list[Path] = []
    for rid in ids:
        try:
            results.append(
                process_note(rid, on_processed=on_processed, model=model)
            )
        except Exception as e:  # noqa: BLE001
            logger.error("加工 %s 失败: %s", rid, e)
    return results
