"""The time budget of one request, set by REQUEST_TIMEOUT.

The traversal and the link source check it before each Solr query. When it runs out, the result is truncated rather
than an error.
"""

import math
import time
from collections.abc import Callable
from dataclasses import dataclass


class BudgetExhausted(Exception):
    """The request's time budget ran out before a query could start."""


@dataclass(frozen=True)
class Budget:
    deadline: float
    clock: Callable[[], float] = time.monotonic

    @classmethod
    def of_seconds(cls, seconds: float, clock: Callable[[], float] = time.monotonic) -> "Budget":
        return cls(deadline=clock() + seconds, clock=clock)

    def expired(self) -> bool:
        return self.clock() >= self.deadline

    def check(self) -> None:
        if self.expired():
            raise BudgetExhausted("the request's time budget ran out")


UNLIMITED = Budget(deadline=math.inf)
