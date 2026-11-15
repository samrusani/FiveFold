"""The price file of live runs: its date, exact prices, and the files it refuses (0004).

A live run's spend cap needs the price of its model, so a model missing from the price
file is refused, and every price is an exact amount, never negative.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from fivefold.domain.prices import (
    PriceFile,
    PriceFileError,
    load_price_file,
    price_file_from_text,
)

EXAMPLE = Path(__file__).resolve().parents[2] / "prices" / "example.yaml"


def prices(input_usd: str = "1", output_usd: str = "2", model: str = "m") -> PriceFile:
    """A price file for the provider `fake`'s model `model`, in USD per million tokens."""
    return price_file_from_text(
        "price_date: 2026-11-15\n"
        "prices:\n"
        "  fake:\n"
        f'    "{model}":\n'
        f"      input_usd_per_million_tokens: {input_usd}\n"
        f"      output_usd_per_million_tokens: {output_usd}\n"
    )


def test_the_example_price_file_prices_ollama_models_at_zero_and_no_api_provider() -> None:
    example = load_price_file(EXAMPLE)
    assert isinstance(example.price_date, date)
    assert set(example.prices) == {"ollama"}
    assert example.prices["ollama"], "it lists Ollama models"
    assert all(price.free for price in example.prices["ollama"].values())
    assert "0 USD per token" in EXAMPLE.read_text(encoding="utf-8")


def test_a_price_file_holds_exact_prices_and_costs_calls_exactly() -> None:
    priced = prices("0.15", "0.6")
    price = priced.price("fake", "m")
    assert price.input_usd_per_million_tokens == Decimal("0.15")
    assert price.output_usd_per_million_tokens == Decimal("0.6")
    assert price.cost_usd(1_000_000, 500_000) == Decimal("0.45")
    assert price.cost_usd(3, 0) == Decimal("0.00000045")
    assert not price.free
    assert priced.price_date == date(2026, 11, 15)
    with pytest.raises(ValueError, match="negative"):
        price.cost_usd(-1, 0)


@pytest.mark.parametrize(
    ("text", "problem"),
    [
        ("prices: {}\n", "price_date"),
        ("price_date: 2026-11-15T10:00:00\nprices: {}\n", "price_date"),
        (
            "price_date: 2026-11-15\nprices: {fake: {m: {input_usd_per_million_tokens: -1,"
            " output_usd_per_million_tokens: 0}}}\n",
            "greater than or equal",
        ),
        (
            "price_date: 2026-11-15\nprices: {fake: {m: {input_usd_per_million_tokens: true,"
            " output_usd_per_million_tokens: 0}}}\n",
            "Decimal input",
        ),
        (
            "price_date: 2026-11-15\nprices: {fake: {m: {input_usd_per_million_tokens: .inf,"
            " output_usd_per_million_tokens: 0}}}\n",
            "finite",
        ),
        (
            "price_date: 2026-11-15\nprices: {fake: {m: {input_usd_per_million_tokens: 0}}}\n",
            "output_usd_per_million_tokens",
        ),
        (
            "price_date: 2026-11-15\nprices: {fake: {m: {input_usd_per_million_tokens: 0,"
            " output_usd_per_million_tokens: 0, cached: 0}}}\n",
            "cached",
        ),
        (
            "price_date: 2026-11-15\nprices: {fake: {1:30: {input_usd_per_million_tokens: 0,"
            " output_usd_per_million_tokens: 0}}}\n",
            "string",
        ),
        ("price_date: [unclosed\n", "not valid YAML"),
    ],
)
def test_a_file_that_is_not_a_price_file_is_refused(text: str, problem: str) -> None:
    with pytest.raises(PriceFileError, match=problem):
        price_file_from_text(text)


def test_a_model_missing_from_the_price_file_is_refused() -> None:
    with pytest.raises(PriceFileError, match="no price for fake:other, so a spend cap"):
        prices().price("fake", "other")
    with pytest.raises(PriceFileError, match="no price for ollama:m"):
        prices().price("ollama", "m")
