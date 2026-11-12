"""The chat model (LangChain's ChatOpenAI) with token counting and a spend cap.

ChatOpenAI handles the HTTP call, retries and the response format. The spend
cap is a callback handler: it reads the token usage LangChain attaches to every
model response and stops the run when the estimated cost passes the cap.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.outputs import LLMResult
from langchain_openai import ChatOpenAI


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Spend:
    """Counts tokens reported by the API and stops the run when the estimated cost passes the cap."""

    price_input_per_m: float
    price_output_per_m: float
    max_usd: float
    input_tokens: int = 0
    output_tokens: int = 0
    calls: int = 0

    @property
    def usd(self) -> float:
        return (self.input_tokens * self.price_input_per_m + self.output_tokens * self.price_output_per_m) / 1_000_000

    def add(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens += input_tokens
        self.output_tokens += output_tokens
        self.calls += 1

    def check(self) -> None:
        if self.usd >= self.max_usd:
            raise BudgetExceeded(f"estimated spend ${self.usd:.4f} reached the cap of ${self.max_usd:.2f}")

    def report(self) -> dict:
        return {
            "calls": self.calls,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_usd": round(self.usd, 4),
            "price_input_per_m": self.price_input_per_m,
            "price_output_per_m": self.price_output_per_m,
        }


class SpendCallback(BaseCallbackHandler):
    raise_error = True  # let BudgetExceeded stop the chain instead of being logged and swallowed

    def __init__(self, spend: Spend) -> None:
        self.spend = spend

    def on_chat_model_start(self, serialized: dict[str, Any], messages: list, **kwargs: Any) -> None:
        self.spend.check()

    def on_llm_end(self, response: LLMResult, **kwargs: Any) -> None:
        for generations in response.generations:
            for generation in generations:
                usage = getattr(getattr(generation, "message", None), "usage_metadata", None) or {}
                self.spend.add(usage.get("input_tokens", 0), usage.get("output_tokens", 0))


def make_chat_model(api_key: str, base_url: str, model: str, spend: Spend, max_tokens: int = 400) -> ChatOpenAI:
    """The key comes from the environment through config.load(). It is never written to disk or logged."""
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        base_url=base_url,
        temperature=0,
        max_tokens=max_tokens,
        max_retries=3,
        timeout=60,
        callbacks=[SpendCallback(spend)],
    )
