"""FastAPI 服务 — 为 Obsidian 侧边栏聊天插件提供 HTTP API。

对应 ob-plugin.md 中 "Smart Connections / Copilot for Obsidian" 的
本地 API Base URL (http://localhost:8000)。

端点:
    GET  /                  健康检查 + 使用说明
    POST /qa                问答流 (核心端点，兼容 OpenAI Chat Completions 格式)
    POST /v1/chat/completions  OpenAI 兼容接口 (供 Copilot/Smart Connections 直接使用)
    POST /ingest            录入原料 (URL 抓取 或 手动文本)
    POST /process           加工原料
    POST /index             重建索引
    GET  /stats             各层统计

启动:
    python kb.py serve [--host 0.0.0.0] [--port 8000]
"""

from __future__ import annotations

import json as _json
import logging
from pathlib import Path
from typing import Any, Generator, Optional

from pydantic import BaseModel, Field

from src.config import settings
from src import raw_store, processor, indexer, qa_engine, wiki_compiler

logger = logging.getLogger(__name__)

# 懒导入 FastAPI (仅 serve 命令需要)
try:
    from fastapi import FastAPI, HTTPException
    from fastapi.responses import JSONResponse, StreamingResponse
    _HAS_FASTAPI = True
except ImportError:
    FastAPI = None  # type: ignore
    HTTPException = Exception  # type: ignore
    JSONResponse = dict  # type: ignore
    StreamingResponse = None  # type: ignore
    _HAS_FASTAPI = False


# ---------- 请求/响应模型 ----------
class QARequest(BaseModel):
    question: str
    auto_rebuild: bool = False


class QAResponse(BaseModel):
    answer: str
    sources: list[str] = Field(default_factory=list)


class IngestRequest(BaseModel):
    url: Optional[str] = None
    text: Optional[str] = None


class IngestResponse(BaseModel):
    raw_id: str
    source_type: str


class ProcessRequest(BaseModel):
    raw_id: Optional[str] = None
    all_pending: bool = False


class ProcessResponse(BaseModel):
    processed: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ChatMessage(BaseModel):
    role: str
    content: str


class ChatCompletionRequest(BaseModel):
    """OpenAI Chat Completions 兼容请求体。"""
    messages: list[ChatMessage]
    model: Optional[str] = None
    stream: bool = False


class ChatCompletionResponse(BaseModel):
    """OpenAI Chat Completions 兼容响应体。"""
    id: str = "kb_chat"
    object: str = "chat.completion"
    choices: list[dict[str, Any]] = Field(default_factory=list)


def _stream_chat_chunks(answer: str) -> Generator[str, None, None]:
    """将完整回答按块生成 OpenAI SSE 流式格式 (chat.completion.chunk)。"""
    chunk_size = 20
    for i in range(0, len(answer), chunk_size):
        piece = answer[i:i + chunk_size]
        data = {
            "id": "kb_chat",
            "object": "chat.completion.chunk",
            "choices": [{
                "index": 0,
                "delta": {"content": piece},
                "finish_reason": None,
            }],
        }
        yield f"data: {_json.dumps(data, ensure_ascii=False)}\n\n"
    done = {
        "id": "kb_chat",
        "object": "chat.completion.chunk",
        "choices": [{
            "index": 0,
            "delta": {},
            "finish_reason": "stop",
        }],
    }
    yield f"data: {_json.dumps(done, ensure_ascii=False)}\n\n"
    yield "data: [DONE]\n\n"


# ---------- 应用工厂 ----------
def create_app() -> "FastAPI":
    if not _HAS_FASTAPI:
        raise ImportError(
            "FastAPI 未安装，请运行: pip install fastapi uvicorn"
        )

    app = FastAPI(
        title="四层认知引擎 API",
        description="个人知识库 HTTP API — 供 Obsidian 侧边栏聊天插件使用",
        version="0.1.0",
    )

    # ---- 健康检查 ----
    @app.get("/")
    def root():
        return {
            "service": "四层认知引擎",
            "version": "0.1.0",
            "endpoints": ["/qa", "/v1/chat/completions", "/ingest", "/process", "/index", "/stats"],
        }

    # ---- 问答流 ----
    @app.post("/qa", response_model=QAResponse)
    def qa(req: QARequest):
        try:
            answer = qa_engine.qa(req.question, auto_rebuild=req.auto_rebuild)
            # 从回答中提取 [[]] 来源
            import re
            sources = re.findall(r"\[\[([^\]]+?)\]\]", answer)
            return QAResponse(answer=answer, sources=sources)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ---- OpenAI 兼容接口 (供 Copilot / Smart Connections 使用) ----
    @app.post("/v1/chat/completions")
    def chat_completions(req: ChatCompletionRequest):
        """将 OpenAI Chat Completions 格式转为知识库问答。

        取 messages 中最后一条 user 消息作为问题，调用 qa_engine，
        返回 OpenAI 兼容格式。支持 stream=true 返回 SSE 流式响应。
        """
        # 提取最后一条 user 消息
        user_messages = [m for m in req.messages if m.role == "user"]
        if not user_messages:
            raise HTTPException(status_code=400, detail="messages 中无 user 消息")
        question = user_messages[-1].content

        try:
            answer = qa_engine.qa(question, auto_rebuild=True)
        except Exception as e:
            answer = f"知识库问答出错: {e}"

        if req.stream:
            return StreamingResponse(
                _stream_chat_chunks(answer),
                media_type="text/event-stream",
            )

        return ChatCompletionResponse(
            id="kb_chat",
            object="chat.completion",
            choices=[{
                "index": 0,
                "message": {"role": "assistant", "content": answer},
                "finish_reason": "stop",
            }],
        )

    # ---- 录入原料 ----
    @app.post("/ingest", response_model=IngestResponse)
    def ingest(req: IngestRequest):
        try:
            if req.url:
                entry = raw_store.save_link(req.url)
            elif req.text:
                entry = raw_store.save_manual(req.text)
            else:
                raise HTTPException(status_code=400, detail="需要 url 或 text")
            return IngestResponse(raw_id=entry.id, source_type=entry.source_type.value)
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

    # ---- 加工原料 ----
    @app.post("/process", response_model=ProcessResponse)
    def process(req: ProcessRequest):
        results: list[str] = []
        errors: list[str] = []
        try:
            if req.all_pending:
                paths = processor.process_pending()
                results = [p.name for p in paths]
            elif req.raw_id:
                p = processor.process_note(req.raw_id)
                results = [p.name]
            else:
                raise HTTPException(status_code=400, detail="需要 raw_id 或 all_pending=true")
        except HTTPException:
            raise
        except Exception as e:
            errors.append(str(e))
        return ProcessResponse(processed=results, errors=errors)

    # ---- 重建索引 ----
    @app.post("/index")
    def rebuild_index():
        stats = indexer.rebuild_index()
        return stats

    # ---- Wiki 编译 ----
    @app.post("/wiki")
    def compile_wiki(all_clusters: bool = True, topic: Optional[str] = None):
        if all_clusters:
            paths = wiki_compiler.compile_all_wiki()
        elif topic:
            p = wiki_compiler.compile_wiki(topic)
            paths = [p] if p else []
        else:
            raise HTTPException(
                status_code=400,
                detail="需要 all_clusters=true 或提供 topic 参数",
            )
        return {"compiled": [p.name for p in paths]}

    # ---- 调度器状态 ----
    @app.get("/scheduler")
    def scheduler_status():
        from src.scheduler.daemon import get_scheduler_info
        return get_scheduler_info()

    @app.post("/scheduler/run")
    def scheduler_run_now():
        """手动触发一次每日全流程。"""
        from src.scheduler import tasks
        results = tasks.run_daily()
        return {
            "results": [
                {
                    "name": r.name,
                    "success": r.success,
                    "entries": r.entries,
                    "duration": round(r.duration, 1),
                    "error": r.error,
                    "detail": r.detail,
                }
                for r in results
            ]
        }

    # ---- 统计 ----
    @app.get("/stats")
    def stats():
        raw_n = len(list(settings.RAW_DIR.glob("*.meta.json")))
        proc_n = len(list(settings.PROCESSED_DIR.glob("*.md")))
        wiki_n = len(list(settings.WIKI_DIR.glob("*.md")))
        idx_n = len(list(settings.INDEX_DIR.glob("*.json")))
        return {
            "raw": raw_n,
            "processed": proc_n,
            "wiki": wiki_n,
            "index_files": idx_n,
        }

    return app


def run_server(host: str = "127.0.0.1", port: int = 8000) -> None:
    """启动 FastAPI 服务 (uvicorn) + 后台定时调度器。"""
    if not _HAS_FASTAPI:
        raise ImportError(
            "FastAPI/uvicorn 未安装，请运行: pip install fastapi uvicorn"
        )
    import uvicorn
    from src.scheduler.daemon import start_scheduler, stop_scheduler

    app = create_app()
    logger.info("启动 API 服务: http://%s:%d", host, port)

    # 启动后台调度器
    scheduler_started = start_scheduler()
    if scheduler_started:
        logger.info("定时调度器随 serve 启动 (GET /scheduler 查看状态)")

    try:
        uvicorn.run(app, host=host, port=port, log_level="info")
    finally:
        stop_scheduler()
