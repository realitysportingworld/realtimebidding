"""Bid shading: the optimum it claims, and train/serve parity with the notebook.

The math tests run on a small hand-written landscape. The parity tests rescore
the notebook's own examples, real test-day requests with the curve and the
shaded bid it computed, and run whenever the exported landscape is deployed.
"""
import math

import pytest

from app.api import replay
from app.ml import ipinyou, shading

WIN_MODEL = shading.load_win_model()
needs_win_model = pytest.mark.skipif(WIN_MODEL is None, reason="landscape artifact not present")
needs_replay = pytest.mark.skipif(
    WIN_MODEL is None or ipinyou.load_model() is None or not ipinyou.load_sample(),
    reason="landscape or replay artifacts not present")

TOY = {
    "features": ["advertiser", "adexchange"], "slope_by": ["advertiser", "adexchange"],
    "intercept": -9.0,
    "weights": {"advertiser": {"1": 0.5}, "adexchange": {"1": 0.0, "2": 1.0}},
    "infrequent": {}, "tag_weights": {"hot": 1.5},
    "slope": {"base": 2.0, "advertiser": {"1": 0.2}, "adexchange": {"2": -0.4}},
    "slope_floor": 0.05,
}


def surplus(value, a, beta, bid):
    return (value - bid) * shading.win_probability(a, beta, bid)


@pytest.mark.parametrize("value", [1.0, 20.0, 80.0, 300.0, 2500.0])
@pytest.mark.parametrize("a, beta", [(-9.0, 2.0), (-14.0, 3.5), (-3.0, 0.6), (2.0, 1.0)])
def test_the_shaded_bid_is_the_surplus_maximum(value, a, beta):
    bid = shading.shade(value, a, beta)
    assert 0 < bid <= value
    best = max((value * i / 20000 for i in range(1, 20001)),
               key=lambda b: surplus(value, a, beta, b))
    assert surplus(value, a, beta, bid) >= surplus(value, a, beta, best) - 1e-9 * value
    assert abs(bid - best) <= value / 10000


def test_a_more_valuable_impression_gets_a_higher_bid():
    bids = [shading.shade(v, -9.0, 2.0) for v in (10, 50, 100, 200, 400)]
    assert bids == sorted(bids)


def test_the_win_probability_rises_with_the_bid_and_is_zero_at_zero():
    probs = [shading.win_probability(-9.0, 2.0, b) for b in (1, 10, 50, 100, 400)]
    assert probs == sorted(probs)
    assert shading.win_probability(-9.0, 2.0, 0) == 0.0


def test_the_curve_adds_feature_tag_and_slope_terms():
    a, beta = shading.curve(TOY, {"advertiser": "1", "adexchange": "2"}, ["hot", "hot"])
    assert a == pytest.approx(-9.0 + 0.5 + 1.0 + 1.5)   # a tag listed twice counts once
    assert beta == pytest.approx(2.0 + 0.2 - 0.4)


def test_an_unseen_slope_value_adds_nothing_and_the_slope_is_floored():
    _, beta = shading.curve(TOY, {"advertiser": "never-seen", "adexchange": "1"}, [])
    assert beta == pytest.approx(2.0)
    floored = {**TOY, "slope": {**TOY["slope"], "base": -1.0}}
    assert shading.curve(floored, {"advertiser": "1", "adexchange": "2"}, [])[1] == 0.05


@needs_win_model
def test_every_parity_example_gets_the_notebooks_curve_and_bid():
    worst_a = worst_beta = worst_bid = 0.0
    for row in WIN_MODEL["parity"]:
        features = {f: row[f] for f in WIN_MODEL["features"]}
        tags = [t for t in row["usertags"].split(",") if t]
        a, beta = shading.curve(WIN_MODEL, features, tags)
        worst_a = max(worst_a, abs(a - row["a"]))
        worst_beta = max(worst_beta, abs(beta - row["beta"]))
        bid = shading.shade(row["value"], row["a"], row["beta"])
        worst_bid = max(worst_bid, abs(bid - row["shaded_bid"]) / max(row["value"], 1e-9))
    assert worst_a < 1e-6, worst_a
    assert worst_beta < 1e-6, worst_beta
    assert worst_bid < 1e-6, worst_bid


@needs_win_model
def test_the_parity_examples_exercise_unseen_values_and_untagged_requests():
    rows = WIN_MODEL["parity"]
    assert any(any(r[f] not in WIN_MODEL["weights"][f] for f in WIN_MODEL["infrequent"])
               for r in rows)
    assert any(not r["usertags"] for r in rows)


@needs_replay
def test_a_first_price_win_pays_the_bid_and_the_shaded_bid_stays_below_value():
    for i in range(0, len(ipinyou.load_sample()), 997):
        fp = replay.replay_auction(id=i, clicked=False)["first_price"]
        market = replay.replay_auction(id=i, clicked=False)["market_price"]
        by = {s["strategy"]: s for s in fp["strategies"]}
        assert by["shaded"]["bid"] <= fp["value"] + 0.005
        for s in fp["strategies"]:
            if s["won"]:
                assert s["paid"] == s["bid"]
                assert s["overpaid"] == pytest.approx(s["bid"] - market, abs=0.01)
            else:
                assert s["paid"] == s["overpaid"] == 0
            assert 0 <= s["win_probability"] <= 1


@needs_replay
def test_the_summary_reports_shading_against_the_tuned_unshaded_bid():
    summary = replay.replay_summary()["first_price"]
    assert summary["model"]["version"] == WIN_MODEL["version"]
    assert {r["budget"] for r in summary["results"]} == {"1/32", "1/8", "1/2"}


@pytest.mark.skipif(ipinyou.load_model() is None or not ipinyou.load_sample(),
                    reason="iPinYou artifacts not present")
def test_without_the_landscape_the_replay_is_second_price_only(monkeypatch):
    monkeypatch.setattr(shading, "load_win_model", lambda: None)
    assert replay.replay_auction(id=0, clicked=False)["first_price"] is None
    assert replay.replay_summary()["first_price"] is None
