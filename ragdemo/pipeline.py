"""The whole question-to-answer path as one LCEL chain: retrieve, apply the refusal checks, answer, attach citations."""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass, field
from operator import itemgetter

from langchain_core.documents import Document
from langchain_core.runnables import Runnable, RunnableBranch, RunnableLambda, RunnablePassthrough

from . import config
from .answer import extractive_chain, generated_chain
from .embed import make_embedder
from .llm import Spend, make_chat_model
from .refusal import REFUSAL, content_terms, decide, decide_reranked
from .rerank import Reranker
from .retrieval import ScoredRetriever, make_retriever
from .store import Store


@dataclass
class Citation:
    document: str
    title: str
    page: int
    heading: str
    score: float
    snippet: str


@dataclass
class ChatResult:
    answer: str
    refused: bool
    mode: str
    top_score: float
    threshold: float
    citations: list[Citation] = field(default_factory=list)
    unknown_terms: list[str] = field(default_factory=list)
    rerank_score: float | None = None
    refused_by: str = ""
    retrieved: list[Document] = field(default_factory=list)
    passages: list[Document] = field(default_factory=list)

    def public(self) -> dict:
        data = asdict(self)
        data.pop("retrieved")
        data.pop("passages")
        return data


def _citation(doc: Document) -> Citation:
    m = doc.metadata
    return Citation(m["document"], m["title"], m["page"], m["heading"], round(m["score"], 4), doc.page_content[:240])


class Pipeline:
    def __init__(
        self,
        retriever: ScoredRetriever,
        answer_chain: Runnable,
        mode: str,
        threshold: float = 0.62,
        strict_threshold: float = 0.0,
        max_citations: int = 3,
        max_context: int = 5,
        reranker: Reranker | None = None,
        rerank_threshold: float = 0.0,
        top_k: int = 5,
    ) -> None:
        self.retriever = retriever  # fetches top_k passages, or the larger rerank pool when a reranker is set
        self.mode = mode  # "extractive" or "generated"
        self.threshold = threshold
        # Applied in place of the threshold when the question uses a word found nowhere in the corpus. 0 turns it off.
        self.strict_threshold = strict_threshold
        self.max_citations = max_citations
        self.max_context = max_context
        self.reranker = reranker
        self.rerank_threshold = rerank_threshold
        self.top_k = top_k  # passages kept after reranking
        self.timings: dict[str, float] = {}
        self.chain: Runnable = (
            RunnablePassthrough.assign(docs=itemgetter("question") | retriever)
            | RunnablePassthrough.assign(decision=RunnableLambda(self._decide))
            | RunnableLambda(self._rerank)
            | RunnableBranch(
                (lambda state: state["decision"].refused, RunnableLambda(self._refuse)),
                RunnablePassthrough.assign(answer=answer_chain) | RunnableLambda(self._finish),
            )
        )

    @property
    def collection(self) -> str:
        return self.retriever.collection

    @property
    def max_passages(self) -> int:
        return self.max_context if self.mode == "generated" else self.max_citations

    def count(self) -> int:
        return self.retriever.store.count()

    def chat(self, question: str) -> ChatResult:
        result = self.chain.invoke({"question": question})
        self.timings = {"rerank_ms": 0.0, **self.timings, **self.retriever.timings}
        return result

    def _decide(self, state: dict):
        self.timings = {}
        unknown = []
        if self.strict_threshold > self.threshold:
            unknown = self.retriever.store.missing_terms(content_terms(state["question"]))
        return decide(state["docs"], unknown, self.threshold, self.strict_threshold, self.max_passages)

    def _rerank(self, state: dict) -> dict:
        """Only questions that pass the cheap checks pay for the cross-encoder."""
        docs, decision = state["docs"], state["decision"]
        if self.reranker and not decision.refused:
            start = time.perf_counter()
            docs = list(self.reranker.compress_documents(docs, state["question"]))
            self.timings["rerank_ms"] = (time.perf_counter() - start) * 1000
            decision = decide_reranked(decision, docs, self.rerank_threshold, self.threshold, self.max_passages)
        return {**state, "docs": docs[: self.top_k], "decision": decision}

    def _refuse(self, state: dict) -> ChatResult:
        d = state["decision"]
        return ChatResult(REFUSAL, True, self.mode, d.top_score, d.limit, [], d.unknown_terms, d.rerank_score, d.refused_by, state["docs"], [])

    def _finish(self, state: dict) -> ChatResult:
        d, answer = state["decision"], state["answer"]
        cited = [d.passages[i] for i in answer.cited]
        citations = [_citation(doc) for doc in cited]
        refused_by = "model" if answer.refused else ""
        return ChatResult(
            answer.text, answer.refused, self.mode, d.top_score, d.limit, citations, d.unknown_terms, d.rerank_score, refused_by, state["docs"], cited
        )


def build_pipeline(settings: config.Settings, spend: Spend | None = None, **overrides) -> Pipeline:
    """Wire the parts together from settings. Keyword overrides replace single settings, for the eval scripts."""
    opt = {**asdict(settings), **overrides}
    embedder = make_embedder(opt["embedding_provider"], opt["embedding_model"])
    store = Store(opt["database_url"], embedder, opt["collection"], use_index=opt["use_index"])
    reranker = Reranker(model=opt["rerank_model"], top_k=opt["top_k"]) if opt["rerank"] else None
    retriever = make_retriever(store, opt["rerank_pool"] if reranker else opt["top_k"])
    mode = opt["answer_mode"]
    if mode == "auto":
        mode = "generated" if opt["openai_api_key"] else "extractive"
    if mode == "generated":
        spend = spend or Spend(opt["price_input_per_m"], opt["price_output_per_m"], opt["max_spend_usd"])
        answer_chain = generated_chain(make_chat_model(opt["openai_api_key"], opt["openai_base_url"], opt["answer_model"], spend))
    else:
        answer_chain = extractive_chain()
    return Pipeline(
        retriever,
        answer_chain,
        mode,
        opt["score_threshold"],
        opt["strict_threshold"],
        opt["max_citations"],
        opt["max_context"],
        reranker,
        opt["rerank_threshold"],
        opt["top_k"],
    )
