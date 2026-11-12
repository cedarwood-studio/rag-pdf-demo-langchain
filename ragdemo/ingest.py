"""Load the PDFs as LangChain Documents, embed them and store them in pgvector.

    python -m ragdemo.ingest --corpus core
    python -m ragdemo.ingest --corpus full
    python -m ragdemo.ingest --corpus core --strategy fixed    # LangChain's stock loader and splitter, stored as "core-fixed"

Two loaders:

- HeadingPDFLoader wraps this repository's own page and heading aware chunker
  (ragdemo/chunking.py) as a LangChain document loader.
- The "fixed" strategy is the usual LangChain starting point: PyPDFLoader, then
  RecursiveCharacterTextSplitter with 1,000 characters and 200 overlap.
"""

from __future__ import annotations

import argparse
import json
import time
from collections.abc import Iterator
from pathlib import Path

from langchain_community.document_loaders import PyPDFLoader
from langchain_core.document_loaders import BaseLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter

from . import config
from .chunking import chunk_pages
from .corpus import DATA, PDF_DIR, ROOT, fetch, load_sources
from .embed import make_embedder
from .pdf import read_pdf
from .store import Store

RESULTS = ROOT / "eval" / "results"


class HeadingPDFLoader(BaseLoader):
    """One Document per chunk, with document, title, page and heading as metadata.

    The text that gets embedded (title, heading path, then the chunk) is kept in
    metadata["embed_text"], so the stored passage stays free of that prefix.
    """

    def __init__(self, path: Path, document: str, title: str) -> None:
        self.path = path
        self.document = document
        self.title = title
        self.pages = 0

    def lazy_load(self) -> Iterator[Document]:
        pages = read_pdf(self.path)
        self.pages = len(pages)
        for chunk in chunk_pages(pages, "heading"):
            metadata = {"document": self.document, "title": self.title, "page": chunk.page, "heading": chunk.heading}
            yield Document(page_content=chunk.text, metadata={**metadata, "embed_text": chunk.embed_text(self.title)})


def load_fixed(path: Path, document: str, title: str) -> tuple[int, list[Document]]:
    """LangChain defaults: one Document per page from PyPDFLoader, split into fixed-size pieces."""
    pages = PyPDFLoader(str(path)).load()
    splitter = RecursiveCharacterTextSplitter(chunk_size=1000, chunk_overlap=200)
    docs = []
    for piece in splitter.split_documents(pages):
        if len(piece.page_content) >= 40:
            metadata = {"document": document, "title": title, "page": piece.metadata["page"] + 1, "heading": ""}
            docs.append(Document(page_content=piece.page_content.replace("\x00", ""), metadata=metadata))
    return len(pages), docs


def ingest_file(store: Store, path: Path, document: str, title: str, strategy: str) -> tuple[int, int]:
    """Returns (pages, chunks)."""
    if strategy == "fixed":
        pages, docs = load_fixed(path, document, title)
        if docs:
            store.vectorstore.add_documents(docs)
        return pages, len(docs)
    loader = HeadingPDFLoader(path, document, title)
    docs = loader.load()
    if docs:
        vectors = store.embeddings.embed_documents([d.metadata.pop("embed_text") for d in docs])
        store.vectorstore.add_embeddings([d.page_content for d in docs], vectors, metadatas=[d.metadata for d in docs])
    return loader.pages, len(docs)


def main() -> None:
    settings = config.load()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--corpus", choices=["core", "full"], default=settings.corpus)
    parser.add_argument("--strategy", choices=["heading", "fixed"], default="heading")
    parser.add_argument("--collection", default=None, help="defaults to the corpus name, with -fixed added for the baseline chunker")
    parser.add_argument("--unless-complete", action="store_true", help="do nothing when an earlier ingest of the collection ran to the end")
    args = parser.parse_args()

    collection = args.collection or (args.corpus if args.strategy == "heading" else f"{args.corpus}-{args.strategy}")
    embedder = make_embedder(settings.embedding_provider, settings.embedding_model, cache=DATA / "cache" / "embeddings.sqlite")
    store = Store(settings.database_url, embedder, collection)
    store.wait_until_ready()
    if args.unless_complete and store.is_complete():
        print(f"collection '{collection}' is complete with {store.count()} chunks, skipping ingest")
        return
    fetch(args.corpus)
    store.init_schema(reset=True)
    start = time.perf_counter()
    pages = chunks = 0
    sources = load_sources(args.corpus)
    per_document = []
    for src in sources:
        began, embedded = time.perf_counter(), embedder.compute_seconds
        p, c = ingest_file(store, PDF_DIR / src["file"], src["file"], src["title"], args.strategy)
        pages += p
        chunks += c
        seconds = round(time.perf_counter() - began, 1)
        per_document.append(
            {
                "file": src["file"],
                "set": src["set"],
                "pages": p,
                "chunks": c,
                "seconds": seconds,
                "embedding_seconds": round(embedder.compute_seconds - embedded, 1),
            }
        )
        print(f"{src['file']}: {p} pages, {c} chunks, {seconds}s", flush=True)
    store.mark_complete()
    total = time.perf_counter() - start
    report = {
        "collection": collection,
        "documents": len(sources),
        "pages": pages,
        "chunks": chunks,
        "total_seconds": round(total, 1),
        "embedded_now": embedder.computed,
        "embedding_seconds": round(embedder.compute_seconds, 1),
        "from_cache": chunks - embedder.computed,
        "parse_and_store_seconds": round(total - embedder.compute_seconds, 1),
        "per_document": per_document,
    }
    RESULTS.mkdir(parents=True, exist_ok=True)
    # A run that embedded every chunk is the cold record. A run that read every vector from the cache is kept beside it,
    # and a mixed run is only printed, so neither overwrites the cold timings.
    if embedder.computed in (0, chunks):
        suffix = "-cached" if embedder.computed == 0 else ""
        (RESULTS / f"ingest-{collection}{suffix}.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "per_document"}))


if __name__ == "__main__":
    main()
