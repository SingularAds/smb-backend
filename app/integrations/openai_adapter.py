import json
import logging
from typing import Any, List, Optional

from app.integrations.langfuse_client import get_async_openai

logger = logging.getLogger(__name__)

class ContentBlock:
    def __init__(self, type: str, text: str = None, id: str = None, name: str = None, input: dict = None):
        self.type = type
        self.text = text
        self.id = id
        self.name = name
        self.input = input

class MessageResponse:
    def __init__(self, content: List[ContentBlock], role: str = "assistant"):
        self.content = content
        self.role = role

class MessagesResource:
    def __init__(self, client: Any):
        self.client = client

    def _convert_tools(self, anthropic_tools: list[dict]) -> list[dict]:
        openai_tools = []
        for tool in anthropic_tools:
            openai_tools.append({
                "type": "function",
                "function": {
                    "name": tool.get("name"),
                    "description": tool.get("description", ""),
                    "parameters": tool.get("input_schema", {"type": "object", "properties": {}})
                }
            })
        return openai_tools

    def _convert_history(self, history: list[dict], system: str) -> list[dict]:
        openai_history = []
        if system:
            openai_history.append({"role": "system", "content": system})
            
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            
            if isinstance(content, str):
                openai_history.append({"role": role, "content": content})
                continue
                
            if isinstance(content, list):
                text_parts = []
                tool_calls = []
                tool_results = []
                
                for block in content:
                    if hasattr(block, "type"):
                        block_type = block.type
                        block_text = getattr(block, "text", "")
                        block_id = getattr(block, "id", "")
                        block_name = getattr(block, "name", "")
                        block_input = getattr(block, "input", {})
                        block_tool_use_id = getattr(block, "tool_use_id", "")
                        block_content = getattr(block, "content", "")
                    else:
                        block_type = block.get("type")
                        block_text = block.get("text", "")
                        block_id = block.get("id")
                        block_name = block.get("name")
                        block_input = block.get("input", {})
                        block_tool_use_id = block.get("tool_use_id")
                        block_content = block.get("content", "")

                    if block_type == "text":
                        if block_text:
                            text_parts.append(block_text)
                    elif block_type == "tool_use":
                        tool_calls.append({
                            "id": block_id,
                            "type": "function",
                            "function": {
                                "name": block_name,
                                "arguments": json.dumps(block_input)
                            }
                        })
                    elif block_type == "tool_result":
                        # Convert to string to avoid complex nestings
                        res_content = block_content
                        if not isinstance(res_content, str):
                            res_content = json.dumps(res_content)
                        tool_results.append({
                            "role": "tool",
                            "tool_call_id": block_tool_use_id,
                            "content": res_content
                        })
                
                if role == "assistant":
                    new_msg = {"role": "assistant"}
                    if text_parts:
                        new_msg["content"] = "\n".join(text_parts)
                    if tool_calls:
                        new_msg["tool_calls"] = tool_calls
                        if "content" not in new_msg:
                            new_msg["content"] = None
                    openai_history.append(new_msg)
                elif role == "user":
                    if text_parts:
                        openai_history.append({"role": "user", "content": "\n".join(text_parts)})
                    for t_res in tool_results:
                        openai_history.append(t_res)
            else:
                openai_history.append(msg)
                
        return openai_history

    async def create(self, model: str, max_tokens: int, messages: list[dict], system: str = "", tools: list[dict] = None, **kwargs) -> MessageResponse:
        openai_messages = self._convert_history(messages, system)

        # Override model to the requested OpenAI model unless specified
        # Assuming the caller might still pass 'claude-haiku...', we intercept it.
        # But for safety, we just force gpt-4o-mini here as per requirements.
        actual_model = "gpt-4o-mini"

        call_kwargs = {
            "model": actual_model,
            "messages": openai_messages,
            "max_tokens": max_tokens,
        }

        # ── Langfuse tracing metadata ─────────────────────────────────────────
        # When the caller passes trace_metadata={...}, forward it to the
        # langfuse-wrapped OpenAI client. The wrapper recognises 'name',
        # 'metadata', 'session_id', 'user_id', 'tags' kwargs and emits a
        # fully-attributed trace. On the vanilla SDK these kwargs are unknown
        # and would raise TypeError, so we strip them when langfuse isn't
        # active (detected by the absence of the 'langfuse' attribute).
        _is_traced_client = self.client.__class__.__module__.startswith("langfuse")
        if "trace_metadata" in kwargs:
            tm = kwargs.pop("trace_metadata") or {}
            if _is_traced_client:
                call_kwargs["name"] = tm.get("name", "customer_ai_reply")
                call_kwargs["metadata"] = {
                    k: v for k, v in tm.items() if k not in {"name", "session_id", "user_id", "tags"}
                }
                if tm.get("session_id"):
                    call_kwargs["session_id"] = tm["session_id"]
                if tm.get("user_id"):
                    call_kwargs["user_id"] = tm["user_id"]
                if tm.get("tags"):
                    call_kwargs["tags"] = tm["tags"]

        if tools:
            call_kwargs["tools"] = self._convert_tools(tools)

        # Translate Anthropic tool_choice to OpenAI tool_choice
        if "tool_choice" in kwargs:
            tc = kwargs["tool_choice"]
            if isinstance(tc, dict):
                if tc.get("type") == "any":
                    call_kwargs["tool_choice"] = "required"
                elif tc.get("type") == "auto":
                    call_kwargs["tool_choice"] = "auto"
                elif tc.get("type") == "tool":
                    call_kwargs["tool_choice"] = {"type": "function", "function": {"name": tc.get("name")}}
            else:
                call_kwargs["tool_choice"] = tc
        # Call OpenAI
        import time
        start_time = time.time()
        resp = await self.client.chat.completions.create(**call_kwargs)
        duration = time.time() - start_time
        logger.info("[LATENCY] OpenAI model completion (%s) took %.3fs", actual_model, duration)
        choice = resp.choices[0]
        msg = choice.message
        
        content_blocks = []
        if msg.content:
            content_blocks.append(ContentBlock(type="text", text=msg.content))
            
        if msg.tool_calls:
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments)
                except Exception as exc:
                    logger.warning("[OpenAIAdapter] Failed to parse tool arguments for %s: %s", tc.function.name, exc)
                    args = {}
                content_blocks.append(ContentBlock(
                    type="tool_use",
                    id=tc.id,
                    name=tc.function.name,
                    input=args
                ))
                
        return MessageResponse(content=content_blocks)

class AsyncOpenAIAnthropicWrapper:
    """
    A drop-in replacement for AsyncAnthropic that routes calls to OpenAI.
    Provides backward compatibility with Anthropic tool schemas and message history formats.

    When LANGFUSE_PUBLIC_KEY/LANGFUSE_SECRET_KEY are set, the underlying
    OpenAI client is automatically wrapped by Langfuse so every chat
    completion is traced (prompt, response, tokens, cost, latency) and
    callers can attach metadata via ``trace_metadata={...}`` kwarg.
    """
    def __init__(self, api_key: str):
        self._openai_client = get_async_openai(api_key=api_key)
        self.messages = MessagesResource(self._openai_client)
