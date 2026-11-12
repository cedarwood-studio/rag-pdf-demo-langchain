"""Retrieval: a LangChain retriever that keeps the similarity score on every document.

The stock retriever (vectorstore.as_retriever()) returns documents without
their scores, and the refusal check needs the scores. This subclass calls the
vector store's scored search and writes the cosine similarity into each
document's metadata.
"""

from __future__ import annotations

import time
from typing import Any

from langchain_core.callbacks import CallbackManagerForRetrieverRun
from langchain_core.documents import Document
from langchain_core.retrievers import BaseRetriever
from pydantic import Field

from .store import Store


class ScoredRetriever(BaseRetriever):
    store: Any  # ragdemo.store.Store
    top_k: int = 5
    timings: dict = Field(default_factory=dict)  # milliseconds spent in the last call, for the benchmark

    @property
    def collection(self) -> str:
        return self.store.collection

    def _get_relevant_documents(self, query: str, *, run_manager: CallbackManagerForRetrieverRun) -> list[Document]:
        start = time.perf_counter()
        vector = self.store.embeddings.embed_query(query)
        embedded = time.perf_counter()
        pairs = self.store.vectorstore.similarity_search_with_score_by_vector(vector, k=self.top_k)
        self.timings.update(embed_ms=(embedded - start) * 1000, search_ms=(time.perf_counter() - embedded) * 1000)
        docs = []
        for doc, distance in pairs:
            doc.metadata["score"] = 1.0 - float(distance)  # the store reports cosine distance
            docs.append(doc)
        return docs


def make_retriever(store: Store, top_k: int = 5) -> ScoredRetriever:
    return ScoredRetriever(store=store, top_k=top_k)
