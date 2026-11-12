"""The answer step as LangChain runnables, with two providers.

extractive_chain needs no key: it returns the best passages word for word,
each with its document and page. generated_chain is prompt | chat model |
string parser, and asks for an answer that cites passages by number. A
generated answer that cites nothing is turned into a refusal.

Both chains take the pipeline state ({"question": ..., "decision": ...}) and
return an Answer.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from operator import itemgetter

from langchain_core.documents import Document
from langchain_core.language_models import BaseChatModel
from langchain_core.output_parsers import StrOutputParser
from langchain_core.prompts import ChatPromptTemplate
from langchain_core.runnables import Runnable, RunnableLambda, RunnablePassthrough

from .refusal import REFUSAL


@dataclass
class Answer:
    text: str
    cited: list[int]  # indexes into the passages given to the answer step
    refused: bool = False


def _source(doc: Document) -> str:
    return f"{doc.metadata['title']}, page {doc.metadata['page']}"


def extractive_answer(passages: list[Document]) -> Answer:
    parts = ["The closest passages in the documents, quoted as written:"]
    for i, doc in enumerate(passages, start=1):
        parts.append(f"[{i}] {_source(doc)}:\n{doc.page_content}")
    return Answer(text="\n\n".join(parts), cited=list(range(len(passages))))


SYSTEM_PROMPT = (
    "You answer questions for a support team using only the numbered passages provided. "
    "Answer in one to three sentences. "
    "Cite every passage you used with its number in square brackets, for example [1]. "
    "Do not use any knowledge that is not in the passages. "
    "If the passages do not state the answer, even if they are on the same subject, "
    f'reply with exactly: "{REFUSAL}"'
)
PROMPT = ChatPromptTemplate.from_messages([("system", SYSTEM_PROMPT), ("human", "Passages:\n\n{passages}\n\nQuestion: {question}")])


def format_passages(passages: list[Document]) -> str:
    return "\n\n".join(f"[{i}] {_source(doc)}\n{doc.page_content}" for i, doc in enumerate(passages, start=1))


def parse_reply(text: str, n_passages: int) -> Answer:
    text = text.strip()
    if REFUSAL.lower().rstrip(".") in text.lower():
        return Answer(text=REFUSAL, cited=[], refused=True)
    cited = sorted({int(n) - 1 for n in re.findall(r"\[(\d+)\]", text) if 0 < int(n) <= n_passages})
    # An answer that cites nothing cannot be checked, so it is not passed on.
    if not cited:
        return Answer(text=REFUSAL, cited=[], refused=True)
    return Answer(text=text, cited=cited)


def extractive_chain() -> Runnable:
    return RunnableLambda(lambda state: extractive_answer(state["decision"].passages))


def generated_chain(model: BaseChatModel) -> Runnable:
    write = (
        {"passages": lambda state: format_passages(state["decision"].passages), "question": itemgetter("question")}
        | PROMPT
        | model
        | StrOutputParser()
    )
    return RunnablePassthrough.assign(reply=write) | RunnableLambda(lambda state: parse_reply(state["reply"], len(state["decision"].passages)))
