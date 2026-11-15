"""The procurement simulator (0002, 0004, 0005).

Suppliers, quotes, budget, inventory, purchase orders and the approvals service, which is
the system of record for authority. The simulator owns ground truth: the effects ledger,
fact history and read log, in schema `sim`. `Simulator` serves the API; its `control`
attribute is the harness's interface for scripted humans and fault plans.
"""

from fivefold.sim.config import SimConfig
from fivefold.sim.service import (
    SIM_ROLE,
    AlreadySetUp,
    ClockError,
    Control,
    ControlError,
    NotSetUp,
    Simulator,
    SimulatorError,
    WrongRole,
    setup_run,
    supplier_directory,
)

__all__ = [
    "SIM_ROLE",
    "AlreadySetUp",
    "ClockError",
    "Control",
    "ControlError",
    "NotSetUp",
    "SimConfig",
    "Simulator",
    "SimulatorError",
    "WrongRole",
    "setup_run",
    "supplier_directory",
]
