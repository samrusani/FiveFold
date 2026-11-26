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
            "1:30 is not a decimal integer: YAML reads it in another base",
        ),
        (
            "price_date: 2026-11-15\nprices: {fake: {m: {input_usd_per_million_tokens: 0,"
            " output_usd_per_million_tokens: 0}}, 7: {}}\n",
            "Input should be a valid string",
        ),
        ("price_date: [unclosed\n", "not valid YAML"),
    ],
)
def test_a_file_that_is_not_a_price_file_is_refused_in_one_line(text: str, problem: str) -> None:
    with pytest.raises(PriceFileError, match=problem) as refused:
        price_file_from_text(text)
    assert "\n" not in str(refused.value), "a command prints the refusal as one line"


def test_a_price_bound_error_is_one_line_even_with_a_multiline_label() -> None:
    text = (
        "price_date: 2026-11-26\nprices:\n  fake:\n"
        '    "model\\nname":\n'
        "      input_usd_per_million_tokens: 1e10\n"
        "      output_usd_per_million_tokens: 0\n"
    )
    with pytest.raises(PriceFileError, match="fake:model name prices must be at most") as refused:
        price_file_from_text(text)
    assert "\n" not in str(refused.value)


def test_an_unquoted_decimal_price_keeps_every_digit() -> None:
    """A YAML float would keep about 17 significant digits; a price keeps all of them,
    quoted or not, and a call costs exactly what they say."""
    digits = "0.123456789012345678901"
    for written in (digits, f"'{digits}'", f'"{digits}"'):
        price = prices(written, "2").price("fake", "m")
        assert price.input_usd_per_million_tokens == Decimal(digits), written
        assert price.cost_usd(1_000_000, 0) == Decimal(digits)


@pytest.mark.parametrize(
    ("written", "value"),
    [
        ("0", 0),
        ("2", 2),
        ("1_000", 1_000),
        ("0.15", "0.15"),
        ("1_000.5", "1000.5"),
        ("1.5e+3", "1500"),
        (".5", "0.5"),
        ("'010'", 10),
    ],
)
def test_integers_decimals_and_quoted_prices_read_as_written(
    written: str, value: int | str
) -> None:
    """Integers and decimals in base 10 read as their text says, and so does a quoted
    price, which pydantic reads as a decimal."""
    assert prices(written).price("fake", "m").input_usd_per_million_tokens == Decimal(value)


@pytest.mark.parametrize(
    ("written", "why"),
    [
        (".inf", ".inf is not a finite decimal"),
        ("-.inf", "-.inf is not a finite decimal"),
        (".nan", ".nan is not a finite decimal"),
        ("1:30.5", "1:30.5 is not a finite decimal"),
        ("1:30", "1:30 is not a decimal integer: YAML reads it in another base"),
        ("010", "010 is not a decimal integer: YAML reads it in another base"),
        ("0x10", "0x10 is not a decimal integer: YAML reads it in another base"),
        ("0b11", "0b11 is not a decimal integer: YAML reads it in another base"),
    ],
)
def test_a_number_that_is_not_a_finite_decimal_as_written_is_refused_in_one_line(
    written: str, why: str
) -> None:
    """YAML reads these as infinity, not a number, or in base 60, 8, 16 or 2, so the
    value would not be the one the text shows in base 10."""
    with pytest.raises(PriceFileError, match=r"^the price file is not a price file: ") as refused:
        prices(written)
    assert why in str(refused.value)
    assert "\n" not in str(refused.value)


@pytest.mark.parametrize("written", ["'1e+1000000'", "'1e-22'", "'0e+1000000'", "1000000001"])
def test_prices_outside_the_file_bounds_are_refused(written: str) -> None:
    with pytest.raises(PriceFileError, match="at most 1,000,000,000 USD"):
        prices(written)


def test_a_model_missing_from_the_price_file_is_refused() -> None:
    with pytest.raises(PriceFileError, match="no price for fake:other, so a spend cap"):
        prices().price("fake", "other")
    with pytest.raises(PriceFileError, match="no price for ollama:m"):
        prices().price("ollama", "m")
