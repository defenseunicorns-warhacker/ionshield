"""Tests for app.models.ml_classifier — featurization, inference, training."""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone


from app.models.ml_classifier import (
    ARTIFACT_PATH,
    CLASSES,
    FEATURE_NAMES,
    _softmax,
    _train_softmax,
    featurize,
    get_classifier,
)
from app.models.ontology import EventType, FusedObservation, Region


def _obs(*, kp=2.0, xray=1e-7, proton=0.1, wind=400.0, bz=0.0):
    return FusedObservation(
        region=Region.from_center(0, 0),
        when=datetime(2026, 4, 26, tzinfo=timezone.utc),
        kp_index=kp,
        bz_nt=bz,
        wind_speed_km_s=wind,
        xray_flux_wm2=xray,
        proton_flux_10mev_pfu=proton,
        f107_sfu=70.0,
        tec_tecu=15.0,
        tec_anomaly_tecu=0.0,
        hmf2_km=300.0,
        nmf2=1.5e11,
    )


def test_featurize_shape_and_log_scale():
    o = _obs(kp=4.5, xray=1e-5, proton=50.0)
    x = featurize(o, [_obs(kp=3.0), o])
    assert len(x) == len(FEATURE_NAMES) == 7
    assert x[0] == 4.5
    assert x[1] == 4.5  # window max
    assert math.isclose(x[2], math.log10(1e-5), rel_tol=1e-9)
    assert math.isclose(x[4], math.log10(50.0), rel_tol=1e-9)


def test_softmax_sums_to_one():
    out = _softmax([1.0, 2.0, 3.0])
    assert math.isclose(sum(out), 1.0, abs_tol=1e-9)


def test_artifact_exists_and_loads():
    """Sanity: the bundled artifact loads and matches the expected schema."""
    assert ARTIFACT_PATH.exists(), "Run `python -m app.models.ml_classifier train` first"
    payload = json.loads(ARTIFACT_PATH.read_text())
    assert payload["classes"] == CLASSES
    assert payload["feature_names"] == FEATURE_NAMES
    assert len(payload["coef"]) == len(CLASSES)
    assert len(payload["coef"][0]) == len(FEATURE_NAMES)
    assert len(payload["mean"]) == len(FEATURE_NAMES)


def test_get_classifier_returns_loaded_model():
    clf = get_classifier()
    assert clf is not None
    assert clf.name == "logreg-v1"


def test_classifier_predicts_background_for_quiet():
    clf = get_classifier()
    quiet = _obs()
    cls, conf = clf.classify([quiet])
    assert cls == EventType.BACKGROUND
    assert 0 < conf <= 1.0


def test_classifier_predicts_geomag_for_high_kp():
    clf = get_classifier()
    storm = _obs(kp=8.0, bz=-10.0, wind=600.0)
    cls, conf = clf.classify([storm])
    assert cls == EventType.GEOMAG_MAIN
    assert conf > 0.5


def test_classifier_predicts_flare_x_for_high_xray():
    clf = get_classifier()
    # X10 flare with the wind / bz signature typical of X-class (synth dist:
    # wind 380-700, bz ~Gaussian(-3, 4)). 5e-4 alone is borderline because
    # secondary features (wind, bz) are also class-discriminative.
    flare = _obs(xray=2e-3, wind=600.0, bz=-5.0)
    cls, conf = clf.classify([flare])
    assert cls == EventType.FLARE_X


def test_classifier_predicts_sep_for_proton_burst():
    clf = get_classifier()
    sep = _obs(proton=5000.0)
    cls, _ = clf.classify([sep])
    assert cls == EventType.SEP_EVENT


def test_softmax_train_separates_two_classes():
    """Quick training sanity check on a tiny separable dataset."""
    X = [[1.0, 0.0], [2.0, 0.0], [3.0, 0.0], [-1.0, 0.0], [-2.0, 0.0]]
    y = [1, 1, 1, 0, 0]
    coef, intercept = _train_softmax(X, y, num_classes=2, epochs=200, lr=0.5)
    # Predict on each row using softmax
    correct = 0
    for xi, yi in zip(X, y):
        logits = [sum(coef[k][j] * xi[j] for j in range(2)) + intercept[k] for k in range(2)]
        pred = logits.index(max(logits))
        if pred == yi:
            correct += 1
    assert correct == len(y)


def test_classify_empty_window_returns_none():
    clf = get_classifier()
    assert clf.classify([]) is None
