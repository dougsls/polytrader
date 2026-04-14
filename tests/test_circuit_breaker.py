"""Circuit breaker — halt após N falhas consecutivas de post_order."""
from src.core.config import load_yaml_config
from src.executor.risk_manager import CONSECUTIVE_POST_FAILS_THRESHOLD, RiskManager


def test_single_failure_does_not_halt():
    rm = RiskManager(load_yaml_config().executor)
    tripped = rm.record_post_fail()
    assert tripped is False
    assert rm.is_halted is False


def test_threshold_failures_trip_halt():
    rm = RiskManager(load_yaml_config().executor)
    tripped_flags = []
    for _ in range(CONSECUTIVE_POST_FAILS_THRESHOLD):
        tripped_flags.append(rm.record_post_fail())
    # Última deve tripar
    assert tripped_flags[-1] is True
    assert rm.is_halted is True
    assert "Consecutive post fails" in (rm.halt_reason or "")


def test_success_resets_counter():
    rm = RiskManager(load_yaml_config().executor)
    for _ in range(CONSECUTIVE_POST_FAILS_THRESHOLD - 1):
        rm.record_post_fail()
    assert rm.is_halted is False
    rm.record_post_success()
    # Próxima falha sozinha não deve tripar (counter zerou)
    assert rm.record_post_fail() is False
    assert rm.is_halted is False


def test_resume_clears_halt_and_counter():
    rm = RiskManager(load_yaml_config().executor)
    for _ in range(CONSECUTIVE_POST_FAILS_THRESHOLD):
        rm.record_post_fail()
    assert rm.is_halted is True
    rm.resume()
    assert rm.is_halted is False
    assert rm.halt_reason is None
    # Novo ciclo de falhas começa do zero
    assert rm.record_post_fail() is False
