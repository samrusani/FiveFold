"""The evaluator (0004): property checks, metrics and the report.

It decides pass or fail only from simulator and harness records, and never calls a model.
It imports only `fivefold.domain`: it reads other components' tables, never their code.
"""
