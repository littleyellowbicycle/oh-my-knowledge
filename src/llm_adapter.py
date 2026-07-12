"""LLM 适配层 - LiteLLM SDK 统一封装。

设计目标 (对应架构 V2.1 "LLM 无关" 原则):
    * 一行代码切换 DeepSeek / GLM / Kimi / Ollama
    * 提供 `chat()` 普通文本调用 与 `chat_json()` 强类型 JSON 调用
    * `chat_json()` 自动注入 JSON Mode 指令，并用 Pydantic 校验
    * 失败自动降级到 `settings.MODEL_FALLBACK` (本地 Ollama)

使用示例:
    from src.llm_adapter import llm
    text = llm.chat("讲个笑话", model=settings.MODEL_QA)
    obj  = llm.chat_json("...", schema=NoteSchema, model=settings.MODEL_PROCESS)
"""

from __future__ import annotations

import json
import logging
import os
from typing import Type, TypeVar

import litellm
from pydantic import BaseModel, ValidationError

from src.config import settings

# LiteLLM 全局日志级别
litellm.set_verbose = False
logging.getLogger("litellm").setLevel(settings.LITELLM_LOG)

# 自定义 API Base (用于 OpenAI 兼容的非标准端点，如 MiniMax M3)
_API_BASE = os.getenv("LITELLM_API_BASE")
if _API_BASE:
    litellm.api_base = _API_BASE

# MiniMax-M3 对长提示返回 finish_reason='abort'，但 LiteLLM 的
# OpenAIChatCompletionFinishReason Literal 未包含该值 → Pydantic 校验失败。
# 此处 patch Choices.__init__ 在传入 Pydantic 前将 'abort' → 'stop'。
import litellm.types.utils as _types_utils

_orig_choices_init = _types_utils.Choices.__init__


def _patched_choices_init(self, finish_reason=None, **kwargs):
    if finish_reason == "abort":
        finish_reason = "stop"
    _orig_choices_init(self, finish_reason=finish_reason, **kwargs)


_types_utils.Choices.__init__ = _patched_choices_init

T = TypeVar("T", bound=BaseModel)

logger = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """LLM 调用或结构化输出校验失败的统一异常。"""


class LLMAdapter:
    """LiteLLM 单点封装。"""

    # ---------- 普通文本调用 ----------
    def chat(
        self,
        prompt: str,
        *,
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
    ) -> str:
        """同步文本对话，返回纯文本响应。

        Args:
            prompt: 用户消息
            model:  LiteLLM 模型字符串；None 则用 MODEL_QA
            system: 可选 system prompt
        """
        model = model or settings.MODEL_QA
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt})

        try:
            resp = litellm.completion(
                model=model,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
                timeout=settings.LLM_TIMEOUT if timeout is None else timeout,
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001 - 适配层统一兜底
            logger.warning("chat() 主模型 %s 失败，降级到 %s: %s", model, settings.MODEL_FALLBACK, e)
            if model == settings.MODEL_FALLBACK:
                raise LLMError(f"本地兜底模型也失败: {e}") from e
            return self.chat(
                prompt,
                model=settings.MODEL_FALLBACK,
                system=system,
                temperature=temperature,
                timeout=timeout,
            )

    # ---------- 结构化 JSON 调用 ----------
    def chat_json(
        self,
        prompt: str,
        *,
        schema: Type[T],
        model: str | None = None,
        system: str | None = None,
        temperature: float | None = None,
        timeout: int | None = None,
    ) -> T:
        """强制 LLM 输出合法 JSON 并用 Pydantic 强校验。

        Args:
            prompt: 用户消息 (描述要抽取的内容)
            schema: Pydantic 模型类，定义目标结构
            model:  LiteLLM 模型字符串；None 则用 MODEL_PROCESS
        Returns:
            schema 的实例
        """
        model = model or settings.MODEL_PROCESS

        schema_json = json.dumps(
            schema.model_json_schema(), ensure_ascii=False, indent=2
        )
        json_instruction = (
            "你必须严格输出一个合法 JSON 对象，符合以下 JSON Schema:\n"
            f"{schema_json}\n"
            "只输出 JSON 本身，禁止包含 markdown 代码块标记、注释或解释性文字。"
        )
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "system", "content": json_instruction})
        messages.append({"role": "user", "content": prompt})

        raw = self._raw_completion(messages, model, temperature, timeout)
        return self._parse_json(
            raw, schema, messages, model, temperature, timeout,
            original_prompt=prompt, original_system=system,
        )

    # ---------- 内部工具 ----------
    def _raw_completion(
        self,
        messages: list[dict],
        model: str,
        temperature: float | None,
        timeout: int | None,
    ) -> str:
        try:
            resp = litellm.completion(
                model=model,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
                timeout=settings.LLM_TIMEOUT if timeout is None else timeout,
                # 部分国产模型支持原生 response_format
                response_format={"type": "json_object"},
            )
            return resp["choices"][0]["message"]["content"].strip()
        except Exception as e:  # noqa: BLE001
            # response_format 不被支持时，回退为普通调用
            logger.debug("response_format 不支持，回退普通调用: %s", e)
            resp = litellm.completion(
                model=model,
                messages=messages,
                temperature=settings.LLM_TEMPERATURE if temperature is None else temperature,
                timeout=settings.LLM_TIMEOUT if timeout is None else timeout,
            )
            return resp["choices"][0]["message"]["content"].strip()

    def _parse_json(
        self,
        raw: str,
        schema: Type[T],
        messages: list[dict],
        model: str,
        temperature: float | None,
        timeout: int | None,
        _attempt: int = 0,
        original_prompt: str = "",
        original_system: str | None = None,
    ) -> T:
        cleaned = self._strip_code_fence(raw)
        try:
            data = json.loads(cleaned)
        except json.JSONDecodeError as e:
            if _attempt >= 1:
                raise LLMError(f"JSON 解析失败两次: {e}\n原文:\n{raw}") from e
            logger.warning("首次 JSON 解析失败，追加修复指令重试: %s", e)
            repair = (
                "你上一条输出不是合法 JSON。请只输出符合 Schema 的 JSON 对象，"
                "不要任何 markdown、代码块标记或注释。"
            )
            messages = messages + [
                {"role": "assistant", "content": raw},
                {"role": "user", "content": repair},
            ]
            raw2 = self._raw_completion(messages, model, temperature, timeout)
            return self._parse_json(
                raw2, schema, messages, model, temperature, timeout, _attempt + 1,
                original_prompt=original_prompt, original_system=original_system,
            )

        try:
            return schema.model_validate(data)
        except ValidationError as e:
            if model == settings.MODEL_FALLBACK:
                raise LLMError(f"Pydantic 校验失败 (兜底模型): {e}\n数据:\n{data}") from e
            logger.warning("Pydantic 校验失败，降级到兜底模型重试: %s", e)
            # 用原始用户 prompt + system 重试，不要用 messages[0] (那是 system/JSON 指令)
            return self.chat_json(
                original_prompt,
                schema=schema,
                system=original_system,
                model=settings.MODEL_FALLBACK,
                temperature=temperature,
                timeout=timeout,
            )

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """去掉 LLM 习惯性包裹的 ```json ... ``` 代码块。"""
        s = text.strip()
        if s.startswith("```"):
            # 去掉首行 ```json 或 ```
            first_nl = s.find("\n")
            if first_nl != -1:
                s = s[first_nl + 1 :]
            if s.rstrip().endswith("```"):
                s = s.rstrip()[:-3]
        return s.strip()


# 模块级单例
llm = LLMAdapter()
