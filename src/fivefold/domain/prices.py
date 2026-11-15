"""The price file of live model runs (records 0004 and 0009).

Record 0004 computes cost "from a price file that records its price date". A live run is
refused without one, and refused when its model is missing from it, so its spend cap can
always be enforced (`fivefold.agents.live`). A price file is YAML:

    price_date: 2026-11-15
    prices:
      ollama:
        "llama3.1:8b":
          input_usd_per_million_tokens: 0
          output_usd_per_million_tokens: 0

- `price_date`: the day the prices were read, as a date.
- `prices`: for each provider and model, the price in USD per million input tokens and per
  million output tokens. A price is an exact decimal, at least 0. Quote model names: YAML
  reads some unquoted names, such as `1:30`, as numbers.

`prices/example.yaml` is the example the repository ships. The evaluator will compute
record 0004's cost metrics from the same format, so this module reads only plain data.

Checked in pydantic 2.14.3: `Field` takes `ge` and `allow_inf_nan` (`fields.py`), and a
`Decimal` field passes them to `decimal_schema` (`pydantic_core/core_schema.py`). Observed
with pydantic 2.14.3 and PyYAML 6.0.3: a YAML float such as 0.15 becomes exactly
`Decimal("0.15")`; booleans, negative amounts and infinity are refused; PyYAML reads
`2026-11-15` as a date, and a `date` field refuses a time of day.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError

MILLION: Final = Decimal(1_000_000)

Usd = Annotated[Decimal, Field(ge=0, allow_inf_nan=False)]
"""An amount in USD: an exact decimal, at least 0."""

Name = Annotated[str, StringConstraints(min_length=1)]


class PriceFileError(ValueError):
    """A file that is not a price file, or a model it has no price for."""


class _PriceModel(BaseModel):
    """Base of the price file's parts: unknown fields are refused and values never change."""

    model_config = ConfigDict(extra="forbid", frozen=True, use_attribute_docstrings=True)


class ModelPrice(_PriceModel):
    """What one model costs, in USD per million tokens."""

    input_usd_per_million_tokens: Usd
    output_usd_per_million_tokens: Usd

    @property
    def free(self) -> bool:
        """Whether neither input nor output costs anything, as with a local model."""
        return self.input_usd_per_million_tokens == 0 and self.output_usd_per_million_tokens == 0

    def cost_usd(self, input_tokens: int, output_tokens: int) -> Decimal:
        """The cost of one model call, in USD, exactly."""
        if input_tokens < 0 or output_tokens < 0:
            raise ValueError(f"token counts cannot be negative: {input_tokens}, {output_tokens}")
        spent = (
            input_tokens * self.input_usd_per_million_tokens
            + output_tokens * self.output_usd_per_million_tokens
        )
        return spent / MILLION


class PriceFile(_PriceModel):
    """A price file: its date, and the price of each provider's models."""

    price_date: date
    """The day the prices were read."""
    prices: dict[Name, dict[Name, ModelPrice]]
    """For each provider, the price of each of its models."""

    def price(self, provider: str, model: str) -> ModelPrice:
        """The price of `provider`'s `model`. Raises `PriceFileError` if the file has none,
        since a spend cap cannot be enforced without it."""
        price = self.prices.get(provider, {}).get(model)
        if price is None:
            raise PriceFileError(
                f"the price file dated {self.price_date} has no price for {provider}:{model},"
                " so a spend cap could not be enforced"
            )
        return price


def price_file_from_text(text: str, where: str = "the price file") -> PriceFile:
    """Read a price file from its YAML text. Raises `PriceFileError` on any problem."""
    try:
        data: object = yaml.safe_load(text)
    except yaml.YAMLError as error:
        raise PriceFileError(f"{where} is not valid YAML: {error}") from None
    try:
        return PriceFile.model_validate(data)
    except ValidationError as error:
        raise PriceFileError(f"{where} is not a price file: {error}") from None


def load_price_file(path: Path) -> PriceFile:
    """Read the price file at `path`."""
    return price_file_from_text(path.read_text(encoding="utf-8"), str(path))
