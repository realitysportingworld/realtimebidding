"""Bid shading for first-price auctions, with the landscape learned on iPinYou.

In a first-price auction the winner pays its own bid, so bidding an
impression's value wins it at no profit. The bid that maximises expected
surplus is

    b* = argmax_b (value - b) * P(win | request, b)

and P(win | request, b), the bid landscape, is what
notebooks/bid-shading.ipynb learns: a logistic curve in log bid,
sigmoid(a(x) + beta(x) * ln b), fitted on won/lost outcomes of exploration
bids only, the one signal a first-price bidder gets. It is exported as plain
JSON in the CTR model's layout and scored here in stdlib math;
tests/test_bid_shading.py reproduces the notebook's curve and shaded bid for
every one of its parity examples.
"""
import functools
import json
import math
from typing import Iterable, Optional

from app.ml import ipinyou
from app.ml.artifacts import ARTIFACT_DIR

WIN_MODEL_FILE = ARTIFACT_DIR / "ipinyou_win_model.json"
# Bisection steps: the answer is within value / 2**24 of the optimum.
ITERATIONS = 24


@functools.lru_cache(maxsize=1)
def load_win_model() -> Optional[dict]:
    try:
        return json.loads(WIN_MODEL_FILE.read_text())
    except FileNotFoundError:
        return None


def curve(model: dict, features: dict, tags: Iterable[str]) -> tuple[float, float]:
    """a(x) and beta(x) of this request's landscape.

    The slope is per advertiser and ad exchange; a value the model never saw
    adds nothing to it, as OneHotEncoder(handle_unknown="ignore") did. A slope
    at or below zero would mean bidding less wins more often, so the notebook
    floors it, and so does this.
    """
    a = ipinyou.linear_score(model, features, tags)
    slope = model["slope"]
    beta = slope["base"] + sum(slope[f].get(features.get(f), 0.0) for f in model["slope_by"])
    return a, max(beta, model["slope_floor"])


def win_probability(a: float, beta: float, bid: float) -> float:
    if bid <= 0:
        return 0.0
    return ipinyou._sigmoid(a + beta * math.log(bid))


def shade(value: float, a: float, beta: float) -> float:
    """The surplus-maximising bid for an impression worth `value`.

    ln((value - b) * F(b)) is concave in b, so the optimum is the one root of
    beta * (1 - F(b)) * (value - b) = b on (0, value]. The same bisection as
    the notebook, step for step, so both land on the same bid.
    """
    lo, hi = 0.0, float(value)
    for _ in range(ITERATIONS):
        mid = (lo + hi) / 2
        f = ipinyou._sigmoid(a + beta * math.log(max(mid, 1e-9)))
        if beta * (1 - f) * (value - mid) > mid:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


def first_price_bids(model: dict, advertiser: str, pctr: float,
                     a: float, beta: float) -> dict[str, float]:
    """What the unshaded and the shaded EV bidder bid on one request.

    Each at its own value level, tuned on the notebook's tune day for half the
    real spend. The unshaded level is already the best constant fraction of
    value, so the shaded bid has to beat a tuned baseline, not a naive one.
    """
    adv = model["bidding"]["advertisers"][advertiser]
    unshaded = adv["fp_ev_level"] * pctr / adv["mean_pctr"]
    value = adv["shade_level"] * pctr / adv["mean_pctr"]
    return {"unshaded": unshaded, "shaded": shade(value, a, beta), "value": value}
