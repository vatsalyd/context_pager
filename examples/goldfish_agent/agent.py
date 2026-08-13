"""Goldfish Agent - reference implementation using the Context Pager MCP relay."""

from __future__ import annotations

import asyncio
import os

from langchain_core.messages import HumanMessage
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_mcp_adapters.client import MultiServerMCPClient
from langgraph.prebuilt import create_react_agent

GOLDFISH_PROMPT = """You are a Goldfish Agent. You have NO in-context long-term memory;
you must re-fetch everything you need for every task.

To persist key insights beyond the current conversation, use
commit_to_long_term_memory(key, insights) — those insights are surfaced back to you
automatically (as recalled_insights) on later search_documents calls.

Workflow:
1. Use search_documents(query, top_k) to find relevant documents. Read
   recalled_insights in the result — they are facts you wrote earlier; treat them as ground truth.
2. Use compress_document(doc_id, page=best_page, focus_area=your_question) to read the
   most relevant page in compressed form.
3. After reading a page, IMMEDIATELY restate the salient facts in YOUR OWN WORDS
   before fetching the next page. Facts you do not restate are lost on the next summarization.
4. Commit critical insights via commit_to_long_term_memory.

Budget: You may make at most 50 tool calls per task. On the 50th, you MUST summarize and stop."""


async def create_goldfish_agent():
    """Create and return a configured Goldfish agent."""
    url = os.getenv("PAGER_MCP_URL", "https://pager.duckdns.org/mcp")
    headers = {}
    if api_key := os.getenv("PAGER_AGENT_KEY"):
        headers["Authorization"] = f"Bearer {api_key}"

    client = MultiServerMCPClient(
        {"pager": {"transport": "http", "url": url, "headers": headers}},
        tool_name_prefix=True,
    )
    tools = await client.get_tools()

    agent = create_react_agent(
        model=ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0),
        tools=tools,
        prompt=GOLDFISH_PROMPT,
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

        for msg in reversed(result.get("messages", [])):
            if getattr(msg, "content", None):
                return msg.content
        return "No response generated."
    finally:
        await client.close()


if __name__ == "__main__":
    task = input("Enter task: ")
    response = asyncio.run(run_task(task))
    print(f"\nResponse:\n{response}")
