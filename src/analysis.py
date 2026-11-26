"""阶跃响应指标与整定建议。纯函数，不依赖 Qt，便于上位机单测。

指标均相对「请求的阶跃」计算：从 y0 跳到 y1，t0 为下发时刻。
上位机采样受 SWD 读取频率限制（默认 80 Hz），时间类指标分辨率约为一个采样周期。
"""
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class StepMetrics:
    amplitude: float          # 请求的阶跃幅值 y1 - y0
    rise_time: float | None   # 10%→90% 上升时间 [s]，未到 90% 为 None
    delay: float | None       # 从下发到 10% 的时间 [s]
    overshoot: float          # 超调 [%]，无超调为 0
    peak_time: float | None   # 峰值时刻（相对 t0）[s]
    settling_time: float | None  # 进入并保持在 ±band 内的时间 [s]，未稳定为 None
    steady_error: float       # 末段平均稳态误差 [% 幅值]，正值表示没到目标
    crossings: int            # 越过目标值的次数，反映振荡
    saturation: float         # 输出顶到限幅的时间占比 [0~1]，无输出数据为 0
    samples: int


def _interp(t0, n0, t1, n1, level):
    if n1 == n0:
        return t1
    return t0 + (level - n0) * (t1 - t0) / (n1 - n0)


def step_metrics(t, y, t0, y0, y1, output=None, limit=None, band=0.02):
    """t, y：采样时间与反馈；output/limit：可选的指令输出与限幅，用来判断是否受限幅制约。"""
    amplitude = y1 - y0
    rows = [(ti, yi, output[i] if output is not None else None) for i, (ti, yi) in enumerate(zip(t, y))
            if ti >= t0 and yi is not None and math.isfinite(yi)]
    if abs(amplitude) < 1e-9 or len(rows) < 4:
        return None
    times = [r[0] - t0 for r in rows]
    norm = [(r[1] - y0) / amplitude for r in rows]

    def first_cross(level):
        for i, n in enumerate(norm):
            if n >= level:
                return _interp(times[i - 1], norm[i - 1], times[i], n, level) if i else times[i]
        return None

    t10, t90 = first_cross(0.1), first_cross(0.9)
    rise = t90 - t10 if t10 is not None and t90 is not None else None
    peak_index = max(range(len(norm)), key=norm.__getitem__)
    overshoot = max(0.0, norm[peak_index] - 1.0) * 100
    tail = norm[-max(3, len(norm) // 5):]
    steady_error = (1.0 - sum(tail) / len(tail)) * 100
    settling = None
    if abs(1.0 - sum(tail) / len(tail)) <= band:
        outside = [i for i, n in enumerate(norm) if abs(n - 1.0) > band]
        settling = times[outside[-1] + 1] if outside and outside[-1] + 1 < len(times) else (0.0 if not outside else None)
    crossings = 0
    reached = False
    for a, b in zip(norm, norm[1:]):
        if (a - 1.0) * (b - 1.0) < 0:
            crossings += 1
            reached = True
        elif b >= 1.0:
            reached = True
    if not reached:
        crossings = 0
    saturation = 0.0
    if limit and output is not None:
        values = [r[2] for r in rows if r[2] is not None and math.isfinite(r[2])]
        if values:
            saturation = sum(abs(v) >= abs(limit) * 0.98 for v in values) / len(values)
    return StepMetrics(amplitude, rise, t10, overshoot, times[peak_index] if overshoot > 0 else None,
                       settling, steady_error, crossings, saturation, len(rows))


def advice(m: StepMetrics | None, loop="速度环"):
    """按指标给出调参方向。只给方向，不代替人判断；返回 [(级别, 文本)]，级别为 ok / warn / bad。"""
    if m is None:
        return [("bad", "采样不足或阶跃幅值为 0，无法分析。请让电机稳定运行后再测，阶跃保持时间不少于 1 s。")]
    out = []
    if m.saturation > 0.3:
        out.append(("warn", f"输出有 {m.saturation * 100:.0f}% 的时间顶在上限，响应快慢主要受限幅制约；"
                            "要更快需提高输出上限，单纯加大 Kp 作用有限。"))
    if m.rise_time is None:
        out.append(("bad", f"没有达到目标的 90%（稳态误差 {m.steady_error:.0f}%）。{loop} Kp 或 Ki 偏小，"
                           "或输出上限太低；也检查负载是否卡住。"))
    if m.overshoot > 25 or m.crossings >= 4:
        out.append(("bad", f"超调 {m.overshoot:.0f}%、越过目标 {m.crossings} 次，振荡明显。"
                           f"先把 {loop} Kp 降 30% 左右，Ki 同步减小；仍振荡再加少量 Kd。"))
    elif m.overshoot > 10:
        out.append(("warn", f"超调 {m.overshoot:.0f}%。{loop} Kp 或 Ki 可降 10~20%，想保持速度可加少量 Kd。"))
    if m.rise_time is not None and m.overshoot < 2 and m.settling_time is not None and m.saturation < 0.3:
        out.append(("ok", f"无超调，上升 {m.rise_time * 1000:.0f} ms。想更快可把 {loop} Kp 加大 20% 再测。"))
    if m.rise_time is not None and abs(m.steady_error) > 3:
        out.append(("warn", f"稳态误差 {m.steady_error:.1f}%，适当增大 {loop} Ki。"))
    if m.rise_time is not None and m.settling_time is None and m.overshoot <= 25:
        out.append(("warn", "保持时间内没有稳定在 ±2% 内：延长保持时间，或增大 Ki 消除慢速爬升。"))
    if not out:
        out.append(("ok", "响应良好：超调小、能稳定到目标。"))
    return out


def format_metrics(m: StepMetrics | None, unit=""):
    if m is None:
        return "—"
    def ms(v):
        return "—" if v is None else f"{v * 1000:.0f} ms"
    return (f"幅值 {m.amplitude:.4g} {unit}  ·  上升 {ms(m.rise_time)}  ·  超调 {m.overshoot:.1f}%  ·  "
            f"调节 {ms(m.settling_time)}  ·  稳态误差 {m.steady_error:.1f}%  ·  {m.samples} 点")
