"""命令行入口 - 四层认知引擎统一 CLI。

命令清单:
    kb ingest -u <url>              抓取链接并落盘 raw/
    kb ingest -t "<text>"           手动输入落盘 raw/
    kb ingest --stdin               从 stdin 读取 (管道时自动检测)
    kb process [--all|--raw <id>]   加工原料为 Obsidian 笔记 (含关联引擎钩子)
    kb index                        全量重建索引层
    kb qa "<question>"              问答流 (查索引->读结论->作答)
    kb wiki [--all|--topic <tag>]   Wiki 编译 (关联簇 >= 3)
    kb wiki --lint                  Wiki 健康自检
    kb serve [--host] [--port]      启动 FastAPI 服务 (供 Obsidian 侧边栏)
    kb list [raw|processed|wiki]    列出条目
    kb stats                        显示各层统计
    kb schedule status              查看定时调度器状态
    kb schedule run [daily|zhihu|trending|pipeline]  手动触发任务

返回码: 0 成功，1 业务错误，2 参数错误。
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from src.config import settings
from src import raw_store
from src import processor
from src import indexer
from src import qa_engine
from src import wiki_compiler
from src import workflow


# ---------- 通用工具 ----------
def _emit(msg: str, *, file=None) -> None:
    print(msg, flush=True, file=file)


def _setup_logging(verbose: bool) -> None:
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )


# ---------- 子命令 ----------
def cmd_ingest(args: argparse.Namespace) -> int:
    if args.url:
        entry = raw_store.save_link(args.url)
        _emit(f"已抓取并归档: {entry.id} (source={entry.source_url})")
        return 0
    # 手动输入
    if args.stdin or (not sys.stdin.isatty() and args.text is None):
        text = sys.stdin.read()
    elif args.text is not None:
        text = args.text
    else:
        _emit("错误: 需要提供 --url 或 --text 或 --stdin", file=sys.stderr)
        return 2
    if not text or not text.strip():
        _emit("错误: 输入内容为空", file=sys.stderr)
        return 2
    entry = raw_store.save_manual(text)
    _emit(f"已归档手动输入: {entry.id}")
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    if args.all:
        paths = processor.process_pending()
        _emit(f"批量加工完成: {len(paths)} 篇")
        for p in paths:
            _emit(f"  -> {p.name}")
        return 0
    if args.raw:
        p = processor.process_note(args.raw)
        _emit(f"加工完成: {p.name}")
        return 0
    _emit("错误: 需要 --all 或 --raw <id>", file=sys.stderr)
    return 2


def cmd_index(args: argparse.Namespace) -> int:
    stats = indexer.rebuild_index()
    _emit(f"索引重建完成: {json.dumps(stats, ensure_ascii=False)}")
    return 0


def cmd_qa(args: argparse.Namespace) -> int:
    answer = qa_engine.qa(args.question, auto_rebuild=args.auto_rebuild)
    _emit(answer)
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    if args.lint:
        report = wiki_compiler.lint_wiki()
        _emit(f"Wiki 自检: {json.dumps(report, ensure_ascii=False)}")
        return 0
    if args.all:
        paths = wiki_compiler.compile_all_wiki(force_llm=args.force_llm)
        _emit(f"编译完成: {len(paths)} 篇综述")
        for p in paths:
            _emit(f"  -> {p.name}")
        return 0
    if args.topic:
        p = wiki_compiler.compile_wiki(args.topic, force_llm=args.force_llm)
        if p:
            _emit(f"编译完成: {p.name}")
            return 0
        _emit("簇节点数不足或无有效笔记，未生成综述")
        return 1
    _emit("错误: 需要 --all / --topic <tag> / --lint", file=sys.stderr)
    return 2


def cmd_list(args: argparse.Namespace) -> int:
    kind = args.kind
    if kind == "raw":
        for rid in raw_store.list_raw():
            _emit(rid)
    elif kind == "processed":
        for p in sorted(settings.PROCESSED_DIR.glob("*.md")):
            _emit(p.name)
    elif kind == "wiki":
        for p in sorted(settings.WIKI_DIR.glob("*.md")):
            _emit(p.name)
    elif kind == "pending":
        for rid in raw_store.iter_pending():
            _emit(rid)
    elif kind == "stubs":
        _list_stubs()
    else:
        _emit(f"未知列表类型: {kind}", file=sys.stderr)
        return 2
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    raw_n = len(list(settings.RAW_DIR.glob("*.meta.json")))
    proc_n = len(list(settings.PROCESSED_DIR.glob("*.md")))
    wiki_n = len(list(settings.WIKI_DIR.glob("*.md")))
    idx_n = len(list(settings.INDEX_DIR.glob("*.json")))
    _emit(f"原料层 raw:      {raw_n} 条")
    _emit(f"加工层 processed: {proc_n} 篇")
    _emit(f"编译层 wiki:     {wiki_n} 篇")
    _emit(f"索引层 index:    {idx_n} 个文件")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    """启动 FastAPI 服务 (供 Obsidian 侧边栏聊天插件使用)。"""
    from src.api_server import run_server
    _emit(f"启动 API 服务: http://{args.host}:{args.port}")
    _emit("供 Obsidian Copilot/Smart Connections 使用的端点:")
    _emit(f"  POST {args.host}:{args.port}/v1/chat/completions  (OpenAI 兼容)")
    _emit(f"  POST {args.host}:{args.port}/qa                   (原生问答)")
    _emit("按 Ctrl+C 停止。")
    run_server(host=args.host, port=args.port)
    return 0


def _list_stubs() -> None:
    """列出所有 stub/index 笔记。"""
    import frontmatter as fm
    stubs: list[str] = []
    indexes: list[str] = []
    for p in sorted(settings.PROCESSED_DIR.glob("*.md")):
        try:
            post = fm.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        nt = str(post.metadata.get("note_type") or "content")
        if nt == "stub":
            stubs.append(p.name)
        elif nt == "index":
            indexes.append(p.name)
    _emit(f"stub  ({len(stubs)} 篇):")
    for n in stubs:
        _emit(f"  - {n}")
    _emit(f"index ({len(indexes)} 篇):")
    for n in indexes:
        _emit(f"  - {n}")


def cmd_migrate(args: argparse.Namespace) -> int:
    """存量数据迁移。"""
    if args.task == "note-types":
        return _migrate_note_types()
    _emit(f"未知迁移任务: {args.task}", file=sys.stderr)
    return 2


def _migrate_note_types() -> int:
    """回填 processed/*.md 的 frontmatter note_type 字段。"""
    import frontmatter as fm
    from src.gateway.channels._shared import detect_raw_type
    from src.raw_store import load_raw

    updated = 0
    skipped = 0
    for p in sorted(settings.PROCESSED_DIR.glob("*.md")):
        try:
            post = fm.loads(p.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            skipped += 1
            continue
        if post.metadata.get("note_type"):
            skipped += 1
            continue
        # 从 frontmatter.source 找对应 raw
        raw_id = post.metadata.get("source")
        detected = "normal"
        if raw_id:
            try:
                entry = load_raw(raw_id)
                detected = detect_raw_type(entry.original_text)
            except Exception:  # noqa: BLE001
                pass
        # 正文启发式: 含"待补全/抓取失败"→ stub
        if detected == "normal":
            body = post.content
            if any(kw in body for kw in ("待补全", "抓取失败", "需要登录态", "HTTP 403")):
                detected = "stub"
        note_type = {"stub": "stub", "index": "index"}.get(detected, "content")
        post.metadata["note_type"] = note_type
        p.write_text(fm.dumps(post, sort_keys=False), encoding="utf-8")
        updated += 1
        _emit(f"  {p.name} -> {note_type}")
    _emit(f"\n回填完成: {updated} 篇更新, {skipped} 篇跳过")
    return 0


def cmd_workflow(args: argparse.Namespace) -> int:
    """一键管线: 录入(可选) → 加工 → 索引 → Wiki 编译。"""
    if args.url:
        result = workflow.ingest_and_process(args.url)
        _emit(f"归档: {result['entries']} 篇")
    else:
        result = {"entries": 0, "pipeline": workflow.run_pipeline()}
    p = result["pipeline"]
    _emit(f"加工: {p['processed']} 篇")
    _emit(f"索引: {p['index']['notes']} 笔记 / {p['index']['tags']} 标签")
    _emit(f"Wiki: {p['wiki']} 篇综述")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """启动 MCP 服务 (stdio 模式，供 AI Agent 通过标准协议调用)。"""
    from src.mcp_server import run_mcp
    _emit("启动 MCP 服务 (stdio)，供 Claude Desktop / Cursor 等 AI Agent 调用...", file=sys.stderr)
    _emit("按 Ctrl+C 停止。", file=sys.stderr)
    run_mcp()
    return 0


def cmd_schedule(args: argparse.Namespace) -> int:
    """定时调度器管理: 查看状态 / 手动触发单任务或全流程。"""
    from src.scheduler import config, tasks

    if args.action == "status":
        from src.scheduler.daemon import get_scheduler_info
        info = get_scheduler_info()
        _emit(f"调度器运行中: {info['running']}")
        _emit(f"时区: {config.TIMEZONE}")
        _emit(f"每日触发: {config.DAILY_HOUR:02d}:{config.DAILY_MINUTE:02d}")
        _emit(f"知乎收藏夹: {len(config.ZHIHU_COLLECTIONS)} 个")
        _emit(f"Trending 语言: {', '.join(config.TRENDING_LANGUAGES)}")
        for job in info["jobs"]:
            _emit(f"  任务 {job['id']} | 下次: {job['next_run']} | {job['trigger']}")
        if not info["jobs"] and not info["running"]:
            _emit("  (调度器未启动 — 运行 kb serve 自动启动)")
        return 0

    if args.action == "run":
        task_name = args.task
        if task_name == "daily":
            results = tasks.run_daily()
            for r in results:
                status = "OK" if r.success else f"FAIL: {r.error}"
                _emit(f"  {r.name} | {r.entries} 篇 | {r.duration:.1f}s | {status}")
                for d in r.detail:
                    _emit(f"    {d}")
            return 0 if all(r.success for r in results) else 1
        # 单任务
        fn_map = {
            "zhihu": tasks.fetch_zhihu_collections,
            "trending": tasks.fetch_github_trending,
            "pipeline": tasks.run_pipeline,
        }
        fn = fn_map.get(task_name)
        if not fn:
            _emit(f"未知任务: {task_name} (可选: daily/zhihu/trending/pipeline)", file=sys.stderr)
            return 2
        r = fn()
        status = "OK" if r.success else f"FAIL: {r.error}"
        _emit(f"  {r.name} | {r.entries} 篇 | {r.duration:.1f}s | {status}")
        for d in r.detail:
            _emit(f"    {d}")
        return 0 if r.success else 1

    _emit(f"未知操作: {args.action}", file=sys.stderr)
    return 2


# ---------- 解析器构建 ----------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="kb",
        description="四层认知引擎 - 个人知识库 CLI",
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="调试日志")
    sub = parser.add_subparsers(dest="command", required=True)

    # ingest
    p = sub.add_parser("ingest", help="录入原料 (链接抓取 / 手动输入)")
    p.add_argument("-u", "--url", help="目标 URL (自动路由抓取)")
    p.add_argument("-t", "--text", help="手动输入文本")
    p.add_argument("--stdin", action="store_true", help="从 stdin 读取 (管道时自动检测)")
    p.set_defaults(func=cmd_ingest)

    # process
    p = sub.add_parser("process", help="加工原料为 Obsidian 笔记")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="加工所有 pending 原料")
    g.add_argument("--raw", help="指定原料 ID 加工")
    p.set_defaults(func=cmd_process)

    # index
    p = sub.add_parser("index", help="全量重建索引层")
    p.set_defaults(func=cmd_index)

    # qa
    p = sub.add_parser("qa", help="问答流")
    p.add_argument("question", help="问题")
    p.add_argument("--auto-rebuild", action="store_true", help="索引为空时自动重建")
    p.set_defaults(func=cmd_qa)

    # wiki
    p = sub.add_parser("wiki", help="Wiki 编译层")
    g = p.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="编译所有达标簇")
    g.add_argument("--topic", help="按标签主题编译")
    g.add_argument("--lint", action="store_true", help="Wiki 健康自检")
    p.add_argument("--force-llm", action="store_true", help="跳过 llmwiki，强制自研编译")
    p.set_defaults(func=cmd_wiki)

    # list
    p = sub.add_parser("list", help="列出条目")
    p.add_argument("kind", choices=["raw", "processed", "wiki", "pending", "stubs"],
                   help="列表类型 (stubs = 占位/索引笔记)")
    p.set_defaults(func=cmd_list)

    # stats
    p = sub.add_parser("stats", help="各层统计")
    p.set_defaults(func=cmd_stats)

    # serve
    p = sub.add_parser("serve", help="启动 FastAPI 服务 (供 Obsidian 侧边栏)")
    p.add_argument("--host", default="127.0.0.1", help="监听地址 (默认 127.0.0.1)")
    p.add_argument("--port", type=int, default=8000, help="端口 (默认 8000)")
    p.set_defaults(func=cmd_serve)

    # workflow
    p = sub.add_parser("workflow", help="一键管线: 录入(可选) → 加工 → 索引 → Wiki")
    p.add_argument("-u", "--url", help="可选，先录入 URL 再跑管线")
    p.set_defaults(func=cmd_workflow)

    # migrate
    p = sub.add_parser("migrate", help="存量数据迁移")
    p.add_argument("task", choices=["note-types"],
                   help="note-types: 回填 frontmatter note_type 字段")
    p.set_defaults(func=cmd_migrate)

    # mcp
    p = sub.add_parser("mcp", help="启动 MCP 服务 (stdio，供 AI Agent 调用)")
    p.set_defaults(func=cmd_mcp)

    # schedule
    p = sub.add_parser("schedule", help="定时调度器管理")
    p.add_argument("action", choices=["status", "run"],
                   help="status: 查看调度状态 | run: 手动触发任务")
    p.add_argument("task", nargs="?", default="daily",
                   help="run 时的任务名: daily(默认) / zhihu / trending / pipeline")
    p.set_defaults(func=cmd_schedule)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    _setup_logging(args.verbose)
    settings.ensure_dirs()
    try:
        return args.func(args)
    except Exception as e:  # noqa: BLE001
        logging.error("命令执行失败: %s", e, exc_info=args.verbose)
        _emit(f"错误: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
