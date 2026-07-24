"""Benchmark runner for Context Pager MCP server."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx


FIXTURES_DIR = Path(__file__).parent.parent / "fixtures"
MCP_URL = "http://localhost:8000/mcp"
DASHBOARD_URL = "http://localhost:8501"


async def init_mcp_session(api_key: str) -> str:
    """Initialize an MCP session and return the session ID."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "benchmark", "version": "1.0"},
        },
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(MCP_URL, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        
        # Extract session ID from headers
        session_id = resp.headers.get("Mcp-Session-Id")
        if not session_id:
            raise RuntimeError("No session ID returned from MCP server")
        
        return session_id


async def call_mcp_tool(
    tool_name: str,
    arguments: dict,
    api_key: str,
    session_id: str,
) -> dict:
    """Call an MCP tool and return the result."""
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Mcp-Session-Id": session_id,
    }

    payload = {
        "jsonrpc": "2.0",
        "id": 2,
        "method": "tools/call",
        "params": {"name": tool_name, "arguments": arguments},
    }

    async with httpx.AsyncClient() as client:
        resp = await client.post(MCP_URL, json=payload, headers=headers, timeout=120)
        resp.raise_for_status()

        # Parse SSE response
        for line in resp.text.split("\n"):
            if line.startswith("data: "):
                data = json.loads(line[6:])
                if "result" in data:
                    content = data["result"].get("content", [])
                    if content:
                        return json.loads(content[0].get("text", "{}"))
        return {}


async def upload_document(
    file_path: Path,
    api_key: str,
    doc_id: str | None = None,
) -> dict:
    """Upload a document for ingestion."""
    headers = {"Authorization": f"Bearer {api_key}"}

    async with httpx.AsyncClient() as client:
        # Check if doc already exists
        if doc_id:
            try:
                resp = await client.get(
                    f"{DASHBOARD_URL}/v1/documents/{doc_id}",
                    headers=headers,
                    timeout=10,
                )
                if resp.status_code == 200:
                    return resp.json()  # Already exists
            except Exception:
                pass

        with open(file_path, "rb") as f:
            files = {"file": (file_path.name, f, "text/plain")}
            data = {}
            if doc_id:
                data["doc_id"] = doc_id

            resp = await client.post(
                f"{DASHBOARD_URL}/v1/documents",
                headers=headers,
                files=files,
                data=data,
                timeout=30,
            )
            resp.raise_for_status()
            return resp.json()


async def wait_for_ingestion(doc_id: str, api_key: str, timeout: int = 60) -> dict:
    """Wait for document ingestion to complete."""
    headers = {"Authorization": f"Bearer {api_key}"}
    start = time.time()

    async with httpx.AsyncClient() as client:
        while time.time() - start < timeout:
            resp = await client.get(
                f"{DASHBOARD_URL}/v1/documents/{doc_id}",
                headers=headers,
                timeout=10,
            )
            status = resp.json()
            if status.get("status") == "ready":
                return status
            await asyncio.sleep(1)

    return {"status": "timeout"}


async def run_benchmark_task(
    task_name: str,
    api_key: str,
) -> dict:
    """Run a single benchmark task and return metrics."""
    print(f"\n{'='*60}")
    print(f"Running benchmark: {task_name}")
    print(f"{'='*60}")

    start_time = time.time()
    results = {
        "task": task_name,
        "start_time": datetime.now().isoformat(),
        "tool_calls": 0,
        "tokens_compressed": 0,
        "cost_saved": 0.0,
        "entities_found": 0,
        "success": False,
        "error": None,
    }

    try:
        # Initialize MCP session
        session_id = await init_mcp_session(api_key)
        print(f"MCP session initialized: {session_id[:8]}...")

        if task_name == "code_audit":
            # Upload and analyze code file
            code_file = FIXTURES_DIR / "code_audit.py"
            upload_result = await upload_document(code_file, api_key, "code_audit_001")
            doc_id = upload_result.get("doc_id", "code_audit_001")

            # Wait for ingestion
            status = await wait_for_ingestion(doc_id, api_key)
            if status.get("status") != "ready":
                raise RuntimeError(f"Ingestion failed: {status}")

            # Fetch entity graph for anti-patterns
            entity_result = await call_mcp_tool(
                "fetch_entity_graph",
                {"query": "Python anti-patterns and bugs", "relation": "MENTIONED_WITH"},
                api_key,
                session_id,
            )
            results["entities_found"] = entity_result.get("metadata", {}).get("entities_returned", 0)
            results["tool_calls"] += 1

            # Compress and analyze
            compress_result = await call_mcp_tool(
                "compress_document",
                {"doc_id": doc_id, "max_return_tokens": 2048},
                api_key,
                session_id,
            )
            results["tokens_compressed"] = compress_result.get("metadata", {}).get("compressed_tokens", 0)
            results["cost_saved"] = compress_result.get("metadata", {}).get("cost_saved_usd", 0)
            results["tool_calls"] += 1

            results["success"] = True

        elif task_name == "financial_report":
            # Upload financial report
            report_file = FIXTURES_DIR / "financial" / "acme_corp_2024.txt"
            upload_result = await upload_document(report_file, api_key, "financial_001")
            doc_id = upload_result.get("doc_id", "financial_001")

            status = await wait_for_ingestion(doc_id, api_key)
            if status.get("status") != "ready":
                raise RuntimeError(f"Ingestion failed: {status}")

            # Fetch entities
            entity_result = await call_mcp_tool(
                "fetch_entity_graph",
                {"query": "financial metrics revenue profit", "relation": "MENTIONED_WITH"},
                api_key,
                session_id,
            )
            results["entities_found"] = entity_result.get("metadata", {}).get("entities_returned", 0)
            results["tool_calls"] += 1

            # Compress document
            compress_result = await call_mcp_tool(
                "compress_document",
                {"doc_id": doc_id, "focus_area": "revenue and profitability", "max_return_tokens": 2048},
                api_key,
                session_id,
            )
            results["tokens_compressed"] = compress_result.get("metadata", {}).get("compressed_tokens", 0)
            results["cost_saved"] = compress_result.get("metadata", {}).get("cost_saved_usd", 0)
            results["tool_calls"] += 1

            results["success"] = True

        elif task_name == "meeting_transcripts":
            # Upload transcript
            transcript_file = FIXTURES_DIR / "transcripts" / "q3_strategy_review.txt"
            upload_result = await upload_document(transcript_file, api_key, "transcript_001")
            doc_id = upload_result.get("doc_id", "transcript_001")

            status = await wait_for_ingestion(doc_id, api_key)
            if status.get("status") != "ready":
                raise RuntimeError(f"Ingestion failed: {status}")

            # Fetch entities
            entity_result = await call_mcp_tool(
                "fetch_entity_graph",
                {"query": "meeting decisions action items", "relation": "MENTIONED_WITH"},
                api_key,
                session_id,
            )
            results["entities_found"] = entity_result.get("metadata", {}).get("entities_returned", 0)
            results["tool_calls"] += 1

            # Compress document
            compress_result = await call_mcp_tool(
                "compress_document",
                {"doc_id": doc_id, "focus_area": "decisions and action items", "max_return_tokens": 2048},
                api_key,
                session_id,
            )
            results["tokens_compressed"] = compress_result.get("metadata", {}).get("compressed_tokens", 0)
            results["cost_saved"] = compress_result.get("metadata", {}).get("cost_saved_usd", 0)
            results["tool_calls"] += 1

            results["success"] = True

    except Exception as e:
        results["error"] = str(e)
        print(f"Error: {e}")

    results["elapsed_seconds"] = time.time() - start_time
    results["end_time"] = datetime.now().isoformat()

    print(f"\nResults:")
    print(f"  Tool calls: {results['tool_calls']}")
    print(f"  Entities found: {results['entities_found']}")
    print(f"  Tokens compressed: {results['tokens_compressed']}")
    print(f"  Cost saved: ${results['cost_saved']:.4f}")
    print(f"  Elapsed: {results['elapsed_seconds']:.1f}s")
    print(f"  Success: {results['success']}")

    return results


async def main():
    """Run all benchmark tasks."""
    import argparse

    parser = argparse.ArgumentParser(description="Context Pager Benchmark Runner")
    parser.add_argument("--api-key", required=True, help="API key for authentication")
    parser.add_argument("--task", choices=["code_audit", "financial_report", "meeting_transcripts", "all"], default="all")
    args = parser.parse_args()

    tasks = ["code_audit", "financial_report", "meeting_transcripts"] if args.task == "all" else [args.task]

    all_results = []
    for task in tasks:
        result = await run_benchmark_task(task, args.api_key)
        all_results.append(result)

    # Save results
    output_file = Path(__file__).parent / "benchmark_results.json"
    with open(output_file, "w") as f:
        json.dump(all_results, f, indent=2)

    print(f"\n{'='*60}")
    print("Benchmark Summary")
    print(f"{'='*60}")
    for r in all_results:
        status = "✓" if r["success"] else "✗"
        print(f"  {'[OK]' if r['success'] else '[FAIL]'} {r['task']}: {r['tool_calls']} calls, {r['entities_found']} entities, ${r['cost_saved']:.4f} saved")

    total_saved = sum(r["cost_saved"] for r in all_results)
    print(f"\nTotal cost saved: ${total_saved:.4f}")
    print(f"Results saved to: {output_file}")


if __name__ == "__main__":
    from datetime import datetime
    asyncio.run(main())
