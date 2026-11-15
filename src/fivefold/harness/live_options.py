"""The command-line options of a live run, shared by the commands that run one (0009).

`python -m fivefold.recorder record` and `python -m fivefold.harness.study` take the same
options:

- `--live PROVIDER:MODEL` runs that live model instead of the scripted one;
- `--spend-cap-usd USD` and `--prices PATH`, which `--live` needs: the spend cap above
  0 USD, and the price file that prices the model;
- `--param NAME=VALUE`, repeated, sets one of the model's parameters, VALUE read as JSON,
  or as text when it is not JSON;
- `--call-limit-s SECONDS` fails a model call that has waited that long for the provider
  (default 600), so a stalled server cannot hold a run.

Live runs sit behind an explicit flag and a spend cap (CLAUDE.md). `live_from_args` checks
everything before a command runs anything, so a refused live run never touches a
database: the flag (`FIVEFOLD_LIVE=1`), the provider, its parameters' names and their form
in the cassette, the cap, the call limit, the model's price, and last the parameters'
values, by building the model, which sends nothing. It builds one spend meter, which
every run of the command shares, so the cap holds across them
(`fivefold.agents.live.SpendMeter`).
"""

from __future__ import annotations

import argparse
import json
from collections.abc import Mapping, Sequence
from decimal import Decimal, InvalidOperation
from pathlib import Path

from pydantic import JsonValue

from fivefold.agents.live import (
    CALL_LIMIT_S,
    SpendMeter,
    check_provider,
    parse_live_name,
    provider_model,
)
from fivefold.domain.prices import load_price_file
from fivefold.harness.models import Live, check_live, check_live_flag
from fivefold.recorder.cassette import plain_object


def usd(text: str) -> Decimal:
    """An amount in USD, from the command line."""
    try:
        return Decimal(text)
    except InvalidOperation:
        raise argparse.ArgumentTypeError(f"not an amount: {text!r}") from None


def model_params(items: Sequence[str]) -> dict[str, JsonValue]:
    """The model's parameters from `NAME=VALUE` items, each VALUE as JSON or as text."""
    params: dict[str, JsonValue] = {}
    for item in items:
        name, equals, value = item.partition("=")
        if not name or not equals:
            raise ValueError(f"a model parameter is NAME=VALUE, not {item!r}")
        try:
            params[name] = json.loads(value)
        except json.JSONDecodeError:
            params[name] = value
    return params


def add_live_options(parser: argparse.ArgumentParser) -> None:
    """Add `--live`, `--spend-cap-usd`, `--prices` and `--param` to `parser`."""
    parser.add_argument(
        "--live",
        metavar="PROVIDER:MODEL",
        help="run this live model instead of the scripted one; needs FIVEFOLD_LIVE=1",
    )
    parser.add_argument(
        "--spend-cap-usd",
        type=usd,
        metavar="USD",
        help="with --live: refuse model calls once the spend reaches this many USD",
    )
    parser.add_argument("--prices", type=Path, metavar="PATH", help="with --live: the price file")
    parser.add_argument(
        "--param",
        action="append",
        default=[],
        metavar="NAME=VALUE",
        help="with --live: a model parameter, VALUE as JSON; repeat for more",
    )
    parser.add_argument(
        "--call-limit-s",
        type=float,
        metavar="SECONDS",
        help=f"with --live: fail a model call that waits longer (default {CALL_LIMIT_S:g})",
    )


def check_live_options(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    """End the command through `parser` when the live options do not go together: the
    spend cap, the price file and parameters without `--live`, or `--live` without a cap
    or a price file."""
    given = (
        args.spend_cap_usd is not None
        or args.prices is not None
        or args.param
        or args.call_limit_s is not None
    )
    if args.live is None and given:
        parser.error("--spend-cap-usd, --prices, --param and --call-limit-s go with --live")
    if args.live is not None and (args.spend_cap_usd is None or args.prices is None):
        parser.error("--live needs --spend-cap-usd and --prices")


def live_model(
    name: str,
    params: Sequence[str],
    *,
    spend_cap_usd: Decimal,
    prices: Path,
    environ: Mapping[str, str],
    limit_s: float = CALL_LIMIT_S,
) -> Live:
    """The live model `name`, `PROVIDER:MODEL`, with the parameters `params` and one spend
    meter and a call limit of `limit_s` seconds, checked in this order before anything
    runs: the flag in `environ`, the provider, its parameters' names and their form in the
    cassette, the cap, the call limit, the model's price in the price file at `prices`, and
    last the parameters' values, by building the model, which sends nothing.

    Raises `ValueError` (`LiveRefused`, `LiveError` or `PriceFileError`) for a refusal,
    and `OSError` when the price file cannot be read.
    """
    check_live_flag(environ)
    provider, model = parse_live_name(name)
    parsed = model_params(params)
    check_provider(provider, parsed)
    plain_object(parsed, "the model's parameters")  # the cassette's form of them
    meter = SpendMeter(spend_cap_usd, load_price_file(prices))
    live = Live(provider, model, parsed, spend=meter, limit_s=limit_s)
    check_live(live, environ)
    provider_model(provider, model, parsed)  # their values; building the model sends nothing
    return live


def live_from_args(args: argparse.Namespace, environ: Mapping[str, str]) -> Live | None:
    """The live model the options name, checked (`live_model`); None without `--live`.

    Call `check_live_options` first.
    """
    if args.live is None:
        return None
    limit_s = CALL_LIMIT_S if args.call_limit_s is None else args.call_limit_s
    return live_model(
        args.live,
        args.param,
        spend_cap_usd=args.spend_cap_usd,
        prices=args.prices,
        environ=environ,
        limit_s=limit_s,
    )
