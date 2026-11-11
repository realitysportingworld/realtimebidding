"""Serve the CTR model trained on the iPinYou auction logs.

Exported by notebooks/ipinyou-replay.ipynb as plain JSON and scored here in
pure stdlib math, like the Avazu model: a weighted sum, a sigmoid, then the
isotonic calibration fitted on the notebook's tune day. tests/test_ipinyou_parity.py
rescores every request in the replay sample and must match the notebook.

This model needs the features of an iPinYou bid request (its exchanges,
regions, domains and user segments), which a visitor typing a URL into the
simulator does not have. So it scores the replayed real requests; the live
simulator keeps the Avazu model, whose features any request can supply.
"""
import bisect
import csv
import functools
import gzip
import json
import math
from typing import Iterable, Optional

from app.ml.artifacts import ARTIFACT_DIR

MODEL_FILE = ARTIFACT_DIR / "ipinyou_ctr_model.json"
SAMPLE_FILE = ARTIFACT_DIR / "ipinyou_replay_sample.csv.gz"


@functools.lru_cache(maxsize=1)
def load_model() -> Optional[dict]:
    try:
        return json.loads(MODEL_FILE.read_text())
    except FileNotFoundError:
        return None


def _sigmoid(z: float) -> float:
    if z >= 0:
        return 1.0 / (1.0 + math.exp(-z))
    e = math.exp(z)
    return e / (1.0 + e)


def raw_ctr(model: dict, features: dict, tags: Iterable[str]) -> float:
    """The logistic regression's probability, before calibration."""
    return _sigmoid(linear_score(model, features, tags))


def linear_score(model: dict, features: dict, tags: Iterable[str]) -> float:
    """Intercept plus the weight of each feature's value and each user tag.

    A value the model has a weight for uses it. Anything else, unseen or too
    rare when the model was fitted, falls into the feature's infrequent bucket
    where it has one and contributes nothing where it does not, which is what
    OneHotEncoder(handle_unknown="infrequent_if_exist") did in training. The
    bid landscape model (app.ml.shading) is laid out the same way.
    """
    z = model["intercept"]
    for feature in model["features"]:
        weight = model["weights"][feature].get(features.get(feature))
        if weight is None:
            weight = model["infrequent"].get(feature, 0.0)
        z += weight
    tag_weights = model["tag_weights"]
    # A tag listed twice is still one tag, as in the training matrix.
    for tag in set(tags):
        z += tag_weights.get(tag, 0.0)
    return z


def calibrate(model: dict, p: float) -> float:
    """Isotonic calibration: linear between breakpoints, clipped at the ends."""
    xs, ys = model["calibration"]["x"], model["calibration"]["y"]
    if p <= xs[0]:
        return ys[0]
    if p >= xs[-1]:
        return ys[-1]
    i = bisect.bisect_right(xs, p)
    x0, x1, y0, y1 = xs[i - 1], xs[i], ys[i - 1], ys[i]
    return y0 + (y1 - y0) * (p - x0) / (x1 - x0)


def predict(model: dict, features: dict, tags: Iterable[str]) -> tuple[float, float]:
    raw = raw_ctr(model, features, tags)
    return raw, calibrate(model, raw)


def bids(model: dict, advertiser: str, pctr: float) -> dict[str, float]:
    """What each strategy bids on one request, at its tuned level.

    The EV bid is level * pCTR / mean pCTR, which is pCTR × a value per click
    of level / mean pCTR: the treatment arm's rule in the engine. The levels
    were tuned for half the real spend, on a day the test never saw.
    """
    a = model["bidding"]["advertisers"][advertiser]
    return {"flat": float(a["flat_level"]),
            "ev": a["ev_level"] * pctr / a["mean_pctr"]}


@functools.lru_cache(maxsize=1)
def load_sample() -> list[dict]:
    """Real test-day requests, with the notebook's pCTR for each."""
    try:
        with gzip.open(SAMPLE_FILE, "rt", encoding="utf-8", newline="") as f:
            rows = list(csv.DictReader(f))
    except FileNotFoundError:
        return []
    feature_names = (load_model() or {}).get("features", [])
    return [{
        "features": {f: row[f] for f in feature_names},
        "tags": [t for t in row["usertags"].split(",") if t],
        "market_price": int(row["market_price"]),
        "floor": int(row["floor"]),
        "clicked": row["click"] == "1",
        "day": row["day"],
        "sampled_as": row["sampled_as"],
        "notebook_raw": float(row["pctr_raw"]),
        "notebook_calibrated": float(row["pctr_cal"]),
        "percentile": float(row["pctr_percentile"]),
    } for row in rows]
