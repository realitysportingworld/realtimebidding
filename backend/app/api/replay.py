"""Replay real iPinYou auctions through the served model, one at a time.

Each request in the sample is a real bid request from 11-12 June 2013 with
the price that won it and whether the ad was clicked. The served model prices
it; both bidding strategies bid at the levels tuned on 10 June; each wins when
it beats the real market price and pays that price. The budgeted replay over
all 3.4M test auctions is the notebook's job; this shows its unit of work.

With the bid landscape deployed, each replay also shows the same request in a
first-price auction, where the winner pays its own bid: the unshaded EV bid
against the shaded one (notebooks/bid-shading.ipynb).
"""
import functools
import math
import random
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

from app.ml import ipinyou, shading

router = APIRouter()


@functools.lru_cache(maxsize=1)
def _uniform_rows() -> list[int]:
    return [i for i, r in enumerate(ipinyou.load_sample()) if r["sampled_as"] == "uniform"]


@functools.lru_cache(maxsize=1)
def _clicked_rows() -> list[int]:
    return [i for i, r in enumerate(ipinyou.load_sample()) if r["clicked"]]


def _require_artifacts():
    model, sample = ipinyou.load_model(), ipinyou.load_sample()
    if model is None or not sample:
        raise HTTPException(status_code=503, detail="iPinYou replay artifacts are not deployed")
    return model, sample


@router.get("/auction")
def replay_auction(
    id: Optional[int] = Query(None, ge=0, description="A sample row; random when omitted"),
    clicked: bool = Query(False, description="Draw from requests that were really clicked"),
):
    model, sample = _require_artifacts()
    if id is None:
        # A random request comes from the uniform draw only. The sample also
        # holds every clicked test request, added so they can be found, and
        # drawing from all of it would show a click on one request in eight
        # instead of one in 1,200.
        id = random.choice(_clicked_rows() if clicked else _uniform_rows())
    if id >= len(sample):
        raise HTTPException(status_code=404, detail="No such replay request")
    row = sample[id]

    raw, calibrated = ipinyou.predict(model, row["features"], row["tags"])
    advertiser = row["features"]["advertiser"]
    market = row["market_price"]
    strategies = []
    for strategy, bid in ipinyou.bids(model, advertiser, calibrated).items():
        won = bid > market
        strategies.append({"strategy": strategy, "bid": round(bid, 2), "won": won,
                           "paid": market if won else 0})
    return {
        "id": id,
        "model": {"name": model["name"], "version": model["version"]},
        "request": {**row["features"], "user_tags": row["tags"], "day": row["day"]},
        "sampled_as": row["sampled_as"],
        "market_price": market,
        "price_unit": model["price_unit"],
        "pctr": {
            "raw": raw,
            "calibrated": calibrated,
            "percentile": row["percentile"],
            # The notebook's own score for this request, so anyone can see the
            # served model agree with the one that was evaluated.
            "notebook_calibrated": row["notebook_calibrated"],
            "mean_for_advertiser": model["bidding"]["advertisers"][advertiser]["mean_pctr"],
        },
        "strategies": strategies,
        "first_price": _first_price(row, advertiser, calibrated),
        "clicked": row["clicked"],
    }


def _first_price(row: dict, advertiser: str, pctr: float) -> Optional[dict]:
    win_model = shading.load_win_model()
    if win_model is None:
        return None
    a, beta = shading.curve(win_model, row["features"], row["tags"])
    bids = shading.first_price_bids(win_model, advertiser, pctr, a, beta)
    market = row["market_price"]
    strategies = []
    for strategy in ("unshaded", "shaded"):
        bid = bids[strategy]
        won = bid > market
        strategies.append({
            "strategy": strategy, "bid": round(bid, 2),
            "win_probability": shading.win_probability(a, beta, bid),
            "won": won, "paid": round(bid, 2) if won else 0,
            # What the win cost above the price that would have won it.
            "overpaid": round(bid - market, 2) if won else 0,
        })
    return {
        "model": {"name": win_model["name"], "version": win_model["version"]},
        "value": round(bids["value"], 2),
        # The bid at which this request is a coin flip, by the landscape.
        "predicted_median_price": round(math.exp(-a / beta), 2),
        "strategies": strategies,
    }


@router.get("/summary")
def replay_summary():
    """The offline evaluation behind the served model, from its own artifact."""
    model, sample = _require_artifacts()
    chosen = next(m for m in model["metrics"]["models"]
                  if m["tune_logloss"] == min(x["tune_logloss"] for x in model["metrics"]["models"]))
    calibration = {c["version"]: c for c in model["metrics"]["calibration"]}
    evaluation = model["offline_evaluation"]
    return {
        "model": {"name": model["name"], "version": model["version"],
                  "trained_at": model["trained_at"], "source": model["source"]},
        "test_auc": chosen["test_auc"],
        "calibration_error": {"raw": calibration["raw"]["ece_relative"],
                              "calibrated": calibration["isotonic"]["ece_relative"]},
        "test_auctions": evaluation["test_auctions"],
        "test_clicks": evaluation["test_clicks"],
        "results": [
            {"budget": r["budget"], "click_lift": r["click_lift"], "lift_ci": r["lift_ci"],
             "ecpc_change": r["ecpc_change"], "ecpc_ci": r["ecpc_ci"]}
            for r in evaluation["summary"] if r["strategy"] == "ev"
        ],
        "first_price": _first_price_summary(),
        "sample": {"requests": len(sample),
                   "uniform": sum(r["sampled_as"] == "uniform" for r in sample),
                   "clicked": sum(r["clicked"] for r in sample)},
    }


def _first_price_summary() -> Optional[dict]:
    win_model = shading.load_win_model()
    if win_model is None:
        return None
    rows = {(r["budget"], r["strategy"]): r for r in win_model["metrics"]["summary"]}
    return {
        "model": {"name": win_model["name"], "version": win_model["version"]},
        "results": [{
            "budget": budget,
            "click_lift": r["lift_vs_fp_ev"], "lift_ci": r["lift_vs_fp_ev_ci"],
            "ecpc_change": r["ecpc_vs_fp_ev"], "ecpc_ci": r["ecpc_vs_fp_ev_ci"],
            "overpaid_share": r["overpaid_share"],
            "unshaded_overpaid_share": rows[budget, "fp_ev"]["overpaid_share"],
        } for (budget, strategy), r in rows.items() if strategy == "fp_shade_ctx"],
    }
