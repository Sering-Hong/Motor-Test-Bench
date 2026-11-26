import math
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from analysis import advice, step_metrics  # noqa: E402

DT = 1 / 80


def series(fn, duration=2.0, t0=0.5):
    t = [i * DT for i in range(int(duration / DT))]
    return t, [0.0 if ti < t0 else fn(ti - t0) for ti in t]


def test_first_order_no_overshoot():
    tau = 0.1
    t, y = series(lambda s: 100 * (1 - math.exp(-s / tau)))
    m = step_metrics(t, y, 0.5, 0, 100)
    assert m.overshoot == 0
    assert abs(m.rise_time - tau * math.log(9)) < 2 * DT
    assert abs(m.steady_error) < 1
    assert m.settling_time is not None and m.settling_time < 0.5
    assert any(level == "ok" for level, _ in advice(m))


def test_underdamped_overshoot_detected():
    zeta, wn = 0.2, 20.0
    wd = wn * math.sqrt(1 - zeta ** 2)
    fn = lambda s: 50 * (1 - math.exp(-zeta * wn * s) * (math.cos(wd * s) + zeta / math.sqrt(1 - zeta ** 2) * math.sin(wd * s)))
    t, y = series(fn)
    m = step_metrics(t, y, 0.5, 0, 50)
    expected = 100 * math.exp(-zeta * math.pi / math.sqrt(1 - zeta ** 2))
    assert abs(m.overshoot - expected) < 6
    assert m.crossings >= 3
    assert advice(m)[0][0] == "bad"


def test_steady_error_and_negative_step():
    t, y = series(lambda s: 100 - 95 * (1 - math.exp(-s / 0.05)))
    m = step_metrics(t, [v if ti >= 0.5 else 100 for ti, v in zip(t, y)], 0.5, 100, 0)
    assert abs(m.steady_error - 5) < 1
    assert m.rise_time is not None
    assert any("Ki" in text for _, text in advice(m))


def test_never_reaches_target_and_saturation():
    t, y = series(lambda s: min(40, 80 * s))
    out = [0 if ti < 0.5 else 16384 for ti in t]
    m = step_metrics(t, y, 0.5, 0, 100, output=out, limit=16384)
    assert m.rise_time is None
    assert m.saturation > 0.9
    texts = " ".join(text for _, text in advice(m))
    assert "上限" in texts and "90%" in texts


def test_invalid_input():
    assert step_metrics([0, 1], [0, 1], 0, 0, 1) is None
    assert step_metrics([0, 1, 2, 3, 4], [0] * 5, 0, 5, 5) is None
    assert advice(None)[0][0] == "bad"
