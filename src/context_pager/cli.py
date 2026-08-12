from __future__ import annotations

import click


@click.group()
def main() -> None:
    """Context Pager: page through compressed documents, save token cost."""


@main.command()
def serve() -> None:
    """Run the AWS relay (thin FastMCP router + WSS bridge endpoint)."""
    from context_pager.relay.server import run_relay

    run_relay()


@main.command()
def bridge() -> None:
    """Run the laptop bridge daemon (models + WSS out + localhost MCP)."""
    from context_pager.bridge.daemon import run_bridge

    run_bridge()


@main.group()
def docs() -> None:
    """Manage the local document library."""


@docs.command("add")
@click.argument("file")
@click.option(
    "--kind",
    default="unstructured",
    type=click.Choice(["unstructured", "structured", "mixed"]),
    help="Source kind used as search metadata.",
)
def docs_add(file: str, kind: str) -> None:
    """Register a file: copy into the library, chunk, embed, index."""
    from context_pager.core.tools import add_document

    add_document(file, kind=kind)


@docs.command("list")
def docs_list() -> None:
    """List registered documents."""
    from context_pager.core.storage import open_library

    with open_library() as lib:
        for row in lib.list_documents():
            print(
                f"{row['doc_id']}\t{row['title']}\t"
                f"{row['kind']}\tchunks={row['chunks']}\tindexed={row['indexed_at']}"
            )


@docs.command("reindex")
@click.argument("doc_id")
def docs_reindex(doc_id: str) -> None:
    """Re-chunk + re-embed a document, keeping the same doc_id."""
    from context_pager.core.tools import reindex_document

    reindex_document(doc_id)


@docs.command("remove")
@click.argument("doc_id")
def docs_remove(doc_id: str) -> None:
    """Remove a document from the library (index + registry + cache)."""
    from context_pager.core.tools import remove_document

    remove_document(doc_id)


@main.command()
def stats() -> None:
    """Show local telemetry: calls, tokens saved, per-doc breakdown."""
    from context_pager.bridge.telemetry import print_stats

    print_stats()


if __name__ == "__main__":
    main()
