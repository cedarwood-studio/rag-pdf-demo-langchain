import pytest
from langchain_core.documents import Document
from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, LLMResult

from ragdemo.answer import generated_chain
from ragdemo.llm import BudgetExceeded, Spend, SpendCallback
from ragdemo.refusal import REFUSAL, Decision


def test_chat_returns_answer_with_document_and_page(client):
    response = client.post("/chat", json={"question": "How often should I descale the kettle?"})
    assert response.status_code == 200
    data = response.json()
    assert data["refused"] is False
    assert data["mode"] == "extractive"
    top = data["citations"][0]
    assert top["document"] == "kettle-guide.pdf"
    assert top["page"] == 2
    assert top["heading"] == "2. Descaling"
    assert "citric acid" in data["answer"]


def test_out_of_corpus_question_is_refused(client):
    response = client.post("/chat", json={"question": "Which airlines fly from Lisbon to Toronto?"})
    data = response.json()
    assert data["refused"] is True
    assert data["answer"] == REFUSAL
    assert data["citations"] == []


def test_empty_question_is_rejected(client):
    assert client.post("/chat", json={"question": ""}).status_code == 422


def test_health_reports_chunk_count(client):
    data = client.get("/health").json()
    assert data["status"] == "ok"
    assert data["chunks"] == 3


def test_chat_page_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert "/chat" in response.text


PASSAGES = [
    Document(page_content="Descale every two months.", metadata={"document": "kettle-guide.pdf", "title": "Kettle Guide", "page": 2, "heading": "2. Descaling", "score": 0.9}),
    Document(page_content="Three years.", metadata={"document": "kettle-guide.pdf", "title": "Kettle Guide", "page": 3, "heading": "3. Warranty", "score": 0.8}),
]
STATE = {"question": "How often?", "decision": Decision(False, 0.9, 0.62, [], PASSAGES)}


def _answer(reply: str):
    return generated_chain(FakeListChatModel(responses=[reply])).invoke(STATE)


def test_generated_answer_keeps_only_cited_passages():
    answer = _answer("Every two months [1].")
    assert answer.cited == [0]
    assert answer.refused is False


def test_generated_answer_without_citation_becomes_refusal():
    answer = _answer("Probably every month.")
    assert answer.refused is True
    assert answer.text == REFUSAL


def test_generated_refusal_is_passed_through():
    answer = _answer(REFUSAL)
    assert answer.refused is True
    assert answer.cited == []


def test_spend_is_counted_and_capped():
    spend = Spend(0.15, 0.60, max_usd=0.0003)
    callback = SpendCallback(spend)
    message = AIMessage(content="ok", usage_metadata={"input_tokens": 1000, "output_tokens": 100, "total_tokens": 1100})
    result = LLMResult(generations=[[ChatGeneration(message=message)]])
    callback.on_chat_model_start({}, [])
    callback.on_llm_end(result)
    assert spend.input_tokens == 1000 and spend.output_tokens == 100
    assert abs(spend.usd - 0.00021) < 1e-9
    callback.on_llm_end(result)  # 0.00042 after this call, over the cap
    with pytest.raises(BudgetExceeded):
        callback.on_chat_model_start({}, [])


def test_question_with_a_word_absent_from_the_corpus_meets_the_strict_threshold(pipeline):
    question = "How often should I descale the kettle in Reykjavik?"
    assert pipeline.chat(question).refused is False
    pipeline.strict_threshold = 0.99
    try:
        guarded = pipeline.chat(question)
        assert guarded.refused is True
        assert guarded.unknown_terms == ["reykjavik"]
        # A question made only of words the corpus knows is not held to the strict threshold.
        assert pipeline.chat("How often should I descale the kettle?").refused is False
    finally:
        pipeline.strict_threshold = 0.0


class OverlapReranker:
    """Stands in for the cross-encoder: scores a passage by the question words it contains."""

    def compress_documents(self, documents, query, callbacks=None):
        words = set(query.lower().replace("?", "").split())
        scored = [
            Document(page_content=d.page_content, metadata={**d.metadata, "rerank_score": float(len(words & set(d.page_content.lower().split())))})
            for d in documents
        ]
        return sorted(scored, key=lambda d: -d.metadata["rerank_score"])


def test_reranker_reorders_and_can_refuse(pipeline):
    pipeline.reranker, pipeline.rerank_threshold = OverlapReranker(), 1.0
    try:
        result = pipeline.chat("How long is the warranty on the heating element of the kettle?")
        assert result.refused is False
        assert result.citations[0].page == 3
        assert result.rerank_score >= 1.0
        pipeline.rerank_threshold = 99.0
        refused = pipeline.chat("How long is the warranty on the heating element of the kettle?")
        assert refused.refused is True
        assert refused.refused_by == "rerank"
    finally:
        pipeline.reranker = None
