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
  million output tokens. A price is an exact decimal from 0 to 1,000,000,000 USD, with at
  most 21 fractional places: an integer, a decimal
  such as `0.15`, or either one quoted. Quote model names: YAML reads some unquoted names,
  such as `1:30`, as numbers.

`prices/example.yaml` is the example the repository ships. The evaluator's report computes
record 0004's cost metrics from the same format (`fivefold.evaluator.report`), so this
module reads only plain data.

The file is read with PyYAML's safe loader, except that a number is read from its text.
A decimal such as `0.15` becomes the `Decimal` its text writes, every digit kept: the safe
loader alone would make it a binary float, which keeps about 17 significant digits,
before any check could see it. An integer such as `2` stays an integer. A number that is
not a finite decimal as written is refused while the file is read: `.inf`, `.nan`, and a
number YAML reads in another base, such as `1:30` (base 60, 90), `1:30.5` or `010` (base 8).

Checked in PyYAML 6.0.3: `SafeLoader.add_constructor(tag, constructor)` copies the
constructor table into the subclass it is called on before it adds one, so the safe
loader itself is unchanged (`yaml/constructor.py`); `construct_scalar(node)` returns the
scalar's text; the float tag `tag:yaml.org,2002:float` covers decimals, `.inf`, `.nan` and
base-60 numbers, and the int tag `tag:yaml.org,2002:int` decimal, binary, octal, hex and
base-60 integers (`yaml/resolver.py`); `load(stream, Loader)` reads one document. Checked
in pydantic 2.14.3: `Field` takes `ge` and `allow_inf_nan` (`fields.py`), and a `Decimal`
field passes them to `decimal_schema` (`pydantic_core/core_schema.py`). Observed with
pydantic 2.14.3 and PyYAML 6.0.3: booleans and negative amounts are refused; PyYAML reads
`2026-11-15` as a date, and a `date` field refuses a time of day.
"""

from __future__ import annotations

import re
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Annotated, Final

import yaml
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from yaml.constructor import ConstructorError

MILLION: Final = Decimal(1_000_000)
MAX_FILE_PRICE: Final = Decimal(1_000_000_000)
MAX_PRICE_PLACES: Final = 21

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


class _NotADecimal(ConstructorError):
    """A YAML number that is not a finite decimal as written."""


class _PriceLoader(yaml.SafeLoader):
    """PyYAML's safe loader, but a number is read from its text: a float as an exact
    decimal, and an integer only in base 10."""


def _exact_decimal(loader: _PriceLoader, node: yaml.ScalarNode) -> Decimal:
    """A YAML float as the `Decimal` its text writes. Underscores between digits are
    dropped, as the safe loader drops them. Raises `_NotADecimal` for `.inf`, `.nan` and a
    base-60 number, which no finite decimal writes."""
    text = loader.construct_scalar(node)
    try:
        value = Decimal(text.replace("_", ""))
    except InvalidOperation:
        value = None
    if value is None or not value.is_finite():
        raise _NotADecimal(None, None, f"{text} is not a finite decimal", node.start_mark)
    return value


_DECIMAL_INTEGER = re.compile(r"[-+]?(?:0|[1-9][0-9_]*)")
"""A YAML 1.1 integer in base 10, as `yaml/resolver.py` writes that case."""


def _decimal_integer(loader: _PriceLoader, node: yaml.ScalarNode) -> int:
    """A YAML integer written in base 10, as the safe loader reads it. Raises
    `_NotADecimal` for one YAML reads in another base: binary, octal, hex or base 60."""
    text = loader.construct_scalar(node)
    if _DECIMAL_INTEGER.fullmatch(text) is None:
        problem = f"{text} is not a decimal integer: YAML reads it in another base"
        raise _NotADecimal(None, None, problem, node.start_mark)
    digits = text.replace("_", "")
    if len(digits.lstrip("+-")) > 10:
        raise _NotADecimal(None, None, "an integer in the price file is too large", node.start_mark)
    return int(digits)


_PriceLoader.add_constructor("tag:yaml.org,2002:float", _exact_decimal)
_PriceLoader.add_constructor("tag:yaml.org,2002:int", _decimal_integer)


def price_file_from_text(text: str, where: str = "the price file") -> PriceFile:
    """Read a price file from its YAML text. Raises `PriceFileError` on any problem, with
    its message on one line, as the commands that read a price file refuse in one line."""
    try:
        data: object = yaml.load(text, Loader=_PriceLoader)
    except _NotADecimal as error:
        raise PriceFileError(f"{where} is not a price file: {_one_line(error)}") from None
    except yaml.YAMLError as error:
        raise PriceFileError(f"{where} is not valid YAML: {_one_line(error)}") from None
    try:
        priced = PriceFile.model_validate(data)
    except ValidationError as error:
        raise PriceFileError(f"{where} is not a price file: {_one_line(error)}") from None
    for provider, models in priced.prices.items():
        for model, one in models.items():
            for amount in (one.input_usd_per_million_tokens, one.output_usd_per_million_tokens):
                exponent = amount.as_tuple().exponent
                assert isinstance(exponent, int)  # ModelPrice already requires a finite Decimal.
                if amount > MAX_FILE_PRICE or not -MAX_PRICE_PLACES <= exponent <= 9:
                    message = (
                        f"{where}: {provider}:{model} prices must be at most"
                        f" {MAX_FILE_PRICE:,} USD per million tokens, with a decimal exponent"
                        f" from {-MAX_PRICE_PLACES} to 9"
                    )
                    raise PriceFileError(_one_line(message))
    return priced


def _one_line(value: str | Exception) -> str:
    """Render a message on one line."""
    return " ".join(str(value).split())


def load_price_file(path: Path) -> PriceFile:
    """Read the price file at `path`."""
    return price_file_from_text(path.read_text(encoding="utf-8"), str(path))
