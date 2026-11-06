"""Tests for app.iati_cloud_dcma.budget: the request-time budget (spec §8, REQUEST_TIMEOUT)."""

import pytest

from app.iati_cloud_dcma.budget import UNLIMITED, Budget, BudgetExhausted
from tests.conftest import FakeClock


def test_unlimited_budget_never_expires():
    assert UNLIMITED.expired() is False
    UNLIMITED.check()


def test_budget_expires_when_the_clock_reaches_the_deadline():
    clock = FakeClock()
    budget = Budget.of_seconds(30, clock=clock)
    assert budget.expired() is False
    clock.now += 29.9
    assert budget.expired() is False
    clock.now += 0.1
    assert budget.expired() is True


def test_check_raises_once_expired():
    clock = FakeClock()
    budget = Budget.of_seconds(1, clock=clock)
    budget.check()
    clock.now += 5
    with pytest.raises(BudgetExhausted):
        budget.check()


def test_budget_uses_monotonic_clock_by_default():
    assert Budget.of_seconds(60).expired() is False
