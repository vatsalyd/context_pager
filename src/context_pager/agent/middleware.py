from __future__ import annotations

import logging
from typing import Any

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage
from langchain_google_genai import ChatGoogleGenerativeAI

logger = logging.getLogger("context_pager.agent.middleware")


class ProtectLatestToolResultMiddleware:
    """
    Runs BEFORE SummarizationMiddleware. Finds the latest ToolMessage,
    self-summarizes it via a cheap model call into a compact AIMessage,
    and replaces the ToolMessage with the summary. SummarizationMiddleware
    then sees the summary (which it keeps under `keep=("messages", 6)`).
    """

    def __init__(self, model: str = "gemini-2.5-flash"):
        self.summarizer = ChatGoogleGenerativeAI(model=model, temperature=0)

    async def abefore_model(self, state: dict, config: dict) -> dict:
        messages = state.get("messages", [])
        if not messages:
            return {}

        # Find last ToolMessage from our MCP tools
        last_tool_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if isinstance(messages[i], ToolMessage):
                name = getattr(messages[i], "name", "") or ""
                if name.startswith("pager_"):
                    last_tool_idx = i
                    break

        if last_tool_idx is None:
            return {}

        tool_msg = messages[last_tool_idx]

        # Summarize the tool result
        summary_prompt = f"""Condense this tool result to its essential facts in <= 500 tokens.
Preserve specific numbers, names, decisions. Drop filler.

Tool: {tool_msg.name}
Result: {tool_msg.content}"""

        try:
            summary = await self.summarizer.ainvoke([HumanMessage(content=summary_prompt)])
            summary_text = summary.content
        except Exception as e:
            logger.warning("Failed to summarize tool result: %s", e)
            return {}

        # Replace ToolMessage with AIMessage(summary)
        new_messages = (
            messages[:last_tool_idx]
            + [AIMessage(content=f"[Tool Result Summary] {summary_text}")]
            + messages[last_tool_idx + 1:]
        )

        return {"messages": new_messages}
