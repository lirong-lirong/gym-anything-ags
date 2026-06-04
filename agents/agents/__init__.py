from __future__ import annotations

from importlib import import_module
from typing import Dict, Tuple


_AGENT_MODULES: Dict[str, Tuple[str, str]] = {
    "ClaudeAgent": (".claude", "ClaudeAgent"),
    "Gemini3Agent": (".claude_gemini", "Gemini3Agent"),
    "GeminiQwen3Agent": (".claude_gemini_qwen3", "GeminiQwen3Agent"),
    "GeminiQwen3AuditAgent": (".claude_gemini_qwen3_audit", "GeminiQwen3AuditAgent"),
    "KimiAzureAgent": (".kimi", "KimiAzureAgent"),
    "KimiDistillAgent": (".kimi_distill", "KimiDistillAgent"),
    "MockDoneAgent": (".mock_done", "MockDoneAgent"),
    "Qwen25VLAgent": (".qwen25vl", "Qwen25VLAgent"),
    "Qwen3VLAgent": (".qwen3vl", "Qwen3VLAgent"),
    "Qwen3VLAuditAgent": (".qwen3vl_audit", "Qwen3VLAuditAgent"),
    "Qwen3VLFixedAgent": (".qwen3vlfixed", "Qwen3VLFixedAgent"),
}

__all__ = list(_AGENT_MODULES)


def __getattr__(name: str):
    try:
        module_name, class_name = _AGENT_MODULES[name]
    except KeyError as exc:
        raise AttributeError(name) from exc
    module = import_module(module_name, __name__)
    value = getattr(module, class_name)
    globals()[name] = value
    return value
