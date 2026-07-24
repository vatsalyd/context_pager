"""Goldfish Agent - Reference implementation using Context Pager MCP server."""

from __future__ import annotations

import os
from typing import Annotated

from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient

from context_pager.agent.middleware import ProtectLatestToolResultMiddleware


GOLDFISH_PROMPT = """You are a Goldfish Agent. You have NO in-context long-term memory;
on summarization, prior conversation collapses to a short summary.

To persist key insights beyond summarization, use commit_to_long_term_memory(key, insights) —
those insights will be surfaced back to you automatically when you fetch a relevant entity graph.

Do not assume any prior conversation is intact; always re-fetch pages you need.

Workflow:
1. Use fetch_entity_graph(query, relation) to find relevant entities (filter by relation type!)
2. Use compress_document(doc_id, focus_area, max_return_tokens) to read compressed pages
3. After reading a page, IMMEDIATELY restate the salient facts in YOUR OWN WORDS before fetching the next page.
   Facts you do not restate get lost on the next summarization.
4. Commit critical insights via commit_to_long_term_memory.

Budget: You may make at most 50 tool calls per task. On the 50th, you MUST summarize and stop."""


async def create_goldfish_agent():
    """Create and return a configured Goldfish agent."""
    from langgraph.prebuilt import create_react_agent

    # Connect to Context Pager MCP server
    client = MultiServerMCPClient(
        {
            "pager": {
                "transport": "http",
                "url": os.getenv("PAGER_MCP_URL", "http://localhost:8000/mcp"),
            }
        },
        tool_name_prefix=True,
    )

    tools = await client.get_tools()

    # Create agent with summarization middleware
    agent = create_react_agent(
        model=ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0),
        tools=tools,
        prompt=GOLDFISH_PROMPT,
        state_modifier=ProtectLatestToolResultMiddleware(),
    )

    return agent, client


async def run_task(task: str) -> str:
    """Run a task with the Goldfish agent and return the response."""
    agent, client = await create_goldfish_agent()

    try:
        result = await agent.ainvoke(
            {"messages": [HumanMessage(content=task)]},
            config={"recursion_limit": 100},
        )

        # Get final response
        messages = result.get("messages", [])
        for msg in reversed(messages):
            if hasattr(msg, "content") and msg.content:
                return msg.content

        return "No response generated."
    finally:
        await client.close()


if __name__ == "__main__":
    import asyncio

    async def main():
        task = input("Enter task: ")
        response = await run_task(task)
        print(f"\nResponse:\n{response}")

    asyncio.run(main())
