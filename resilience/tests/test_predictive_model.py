"""Tests for predictive_failure_model.py"""
import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))
from predictive_failure_model import PredictiveFailureModel, FailureRisk


def _make_model():
    alerts = []
    async def alert(msg):
        alerts.append(msg)
    model = PredictiveFailureModel(telegram_alert_fn=alert, db_path="/tmp/test_predictions.db")
    return model, alerts


def test_record_metric():
    model, _ = _make_model()
    model.record_metric("orats_response_time_ms", 1200)
    model.record_metric("orats_response_time_ms", 1600)
    vals = model._get_recent("orats_response_time_ms", 5)
    assert 1200 in vals
    assert 1600 in vals


def test_evaluate_orats_degradation_no_trigger():
    model, _ = _make_model()
    # Low response times — no risk
    for _ in range(10):
        model.record_metric("orats_response_time_ms", 500)
    risk = model._evaluate_orats_degradation()
    assert risk is None


def test_evaluate_orats_degradation_triggers():
    model, _ = _make_model()
    for _ in range(10):
        model.record_metric("orats_response_time_ms", 2000)
        model.record_metric("orats_error_rate", 0.10)
    risk = model._evaluate_orats_degradation()
    assert risk is not None
    assert risk.failure_class == "ORATS_DEGRADATION"
    assert risk.risk_score > 0.65


def test_evaluate_alpaca_rate_limiting():
    model, _ = _make_model()
    for _ in range(5):
        model.record_metric("alpaca_429_count", 1)
    risk = model._evaluate_alpaca_rate_limiting()
    assert risk is not None
    assert risk.risk_score > 0.65


def test_evaluate_vix_spike():
    model, _ = _make_model()
    for vix in [18, 20, 22, 25, 30]:
        model.record_metric("vix_level", float(vix))
    risk = model._evaluate_vix_spike_incoming()
    assert risk is not None
    assert risk.failure_class == "VIX_SPIKE_INCOMING"


def test_run_cycle_returns_risks():
    model, _ = _make_model()
    # Inject high-risk metrics
    for _ in range(10):
        model.record_metric("alpaca_429_count", 2)
        model.record_metric("orats_response_time_ms", 3000)
    risks = asyncio.get_event_loop().run_until_complete(model.run_cycle())
    assert isinstance(risks, list)
    # At least one risk should be returned
    assert any(r.risk_score > 0.3 for r in risks)


def test_eod_report():
    model, _ = _make_model()
    report = model.get_eod_prediction_report()
    assert "total_interventions" in report
    assert "interventions_by_class" in report


def test_get_recent_empty():
    model, _ = _make_model()
    assert model._get_recent("nonexistent_metric", 5) == []
