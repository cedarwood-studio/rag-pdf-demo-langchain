"""Second-stage ranking with a cross-encoder, as a LangChain document compressor.

Vector search compares the question with each passage separately. A
cross-encoder reads the question and one passage together and scores how well
the passage answers the question. It is slower (it runs once per candidate
passage) and more accurate, so it is applied only to the candidates that
vector search returns.

The best cross-encoder score is also used as a refusal check: a passage can be
about the same subject as the question without answering it, and this score
separates those cases better than cosine similarity does.

LangChain ships a CrossEncoderReranker, but it loads models through
sentence-transformers and PyTorch. This class keeps the ONNX model that the
plain-Python implementation uses, behind LangChain's BaseDocumentCompressor
interface.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from langchain_core.callbacks import Callbacks
from langchain_core.documents import BaseDocumentCompressor, Document
from pydantic import PrivateAttr


class Reranker(BaseDocumentCompressor):
    model: str = "Xenova/ms-marco-MiniLM-L-6-v2"
    top_k: int = 5
    _encoder: Any = PrivateAttr(default=None)

    def model_post_init(self, context: Any) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self._encoder = TextCrossEncoder(model_name=self.model)

    def compress_documents(self, documents: Sequence[Document], query: str, callbacks: Callbacks | None = None) -> Sequence[Document]:
        """The top_k documents by cross-encoder score, best first, each with metadata["rerank_score"]."""
        if not documents:
            return []
        scores = list(self._encoder.rerank(query, [d.page_content for d in documents]))
        ranked = sorted(zip(documents, scores, strict=True), key=lambda pair: -pair[1])
        return [Document(page_content=d.page_content, metadata={**d.metadata, "rerank_score": float(s)}) for d, s in ranked[: self.top_k]]
