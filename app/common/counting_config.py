"""Validate counting geometry at every pipeline entry point."""
import math


def _point(point, label):
    if not isinstance(point, (list, tuple)) or len(point) != 2:
        raise ValueError(f"{label} 必须是两个坐标")
    try:
        values = [float(value) for value in point]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} 坐标格式无效") from exc
    if any(not math.isfinite(value) or not 0 <= value <= 1 for value in values):
        raise ValueError(f"{label} 坐标必须为 [0,1] 内的有限数")
    return values


def _orient(a, b, c):
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _on_segment(a, b, p):
    return (abs(_orient(a, b, p)) <= 1e-12
            and min(a[0], b[0]) - 1e-12 <= p[0] <= max(a[0], b[0]) + 1e-12
            and min(a[1], b[1]) - 1e-12 <= p[1] <= max(a[1], b[1]) + 1e-12)


def _intersects(a, b, c, d):
    o1, o2, o3, o4 = _orient(a, b, c), _orient(a, b, d), _orient(c, d, a), _orient(c, d, b)
    if ((o1 > 1e-12 and o2 < -1e-12 or o1 < -1e-12 and o2 > 1e-12)
            and (o3 > 1e-12 and o4 < -1e-12 or o3 < -1e-12 and o4 > 1e-12)):
        return True
    return any((_on_segment(a, b, c), _on_segment(a, b, d),
                _on_segment(c, d, a), _on_segment(c, d, b)))


def effective_line_intervals(line, roi):
    from ..ai.geometry_engine import GeometryEngine
    from ..ai.roi_detector import ROIDetector
    detector = ROIDetector(GeometryEngine(1000, 1000))
    detector.set_roi(roi)
    points = [[p[0] * 1000, p[1] * 1000] for p in line]
    return detector.clip_line_intervals_to_roi(*points)


def validate_counting_geometry(line, anchor=None, roi=None, *, require_line=True):
    """Return normalized geometry; an absent ROI is explicitly the full frame."""
    if line is None:
        if require_line:
            raise ValueError("必须配置计数线")
        normalized_line = None
    else:
        if not isinstance(line, (list, tuple)) or len(line) != 2:
            raise ValueError("计数线必须包含两个点")
        normalized_line = [_point(point, "计数线") for point in line]
        if normalized_line[0] == normalized_line[1]:
            raise ValueError("计数线两个端点不能重合")

    normalized_anchor = _point(anchor, "内侧锚点") if anchor is not None else None
    if normalized_line is not None:
        # 与计数器的默认锚点保持一致，但不能接受默认锚点恰好在线上。
        effective_anchor = normalized_anchor or [0.9, 0.5]
        if abs(_orient(*normalized_line, effective_anchor)) <= 1e-12:
            raise ValueError("内侧锚点位于计数线上，请明确设置锚点")

    normalized_roi = None
    if roi is not None:
        if not isinstance(roi, (list, tuple)) or len(roi) < 3:
            raise ValueError("ROI 至少需要三个顶点")
        normalized_roi = [_point(point, "ROI") for point in roi]
        n = len(normalized_roi)
        if len({tuple(point) for point in normalized_roi}) != n:
            raise ValueError("ROI 顶点不能重复")
        area = sum(normalized_roi[i][0] * normalized_roi[(i + 1) % n][1]
                   - normalized_roi[(i + 1) % n][0] * normalized_roi[i][1] for i in range(n))
        if abs(area) <= 1e-12:
            raise ValueError("ROI 面积必须大于零")
        for i in range(n):
            a, b, c = normalized_roi[i - 1], normalized_roi[i], normalized_roi[(i + 1) % n]
            if abs(_orient(a, b, c)) <= 1e-12 and ((b[0] - a[0]) * (c[0] - b[0]) + (b[1] - a[1]) * (c[1] - b[1])) < 0:
                raise ValueError("ROI 相邻边不能重叠")
            for j in range(i + 1, n):
                if j == i + 1 or (i == 0 and j == n - 1):
                    continue
                if _intersects(normalized_roi[i], normalized_roi[(i + 1) % n],
                               normalized_roi[j], normalized_roi[(j + 1) % n]):
                    raise ValueError("ROI 多边形不能自交或自接")
        if normalized_line is not None:
            # Reuse the exact clipping semantics used by the counter, in pixel-like scale.
            if not effective_line_intervals(normalized_line, normalized_roi):
                raise ValueError("计数线与 ROI 没有有效重合线段")
    return normalized_line, normalized_anchor, normalized_roi


def parse_counting_config(line_coords, anchor_coords, roi_coords, *, require_line=True):
    def parse(value, name, expected=None):
        if value is None or not str(value).strip():
            return None
        try:
            parts = [float(item.strip()) for item in value.split(',')]
        except (ValueError, AttributeError) as exc:
            raise ValueError(f"{name} 格式无效") from exc
        if expected is not None and len(parts) != expected or len(parts) % 2:
            raise ValueError(f"{name} 坐标数量无效")
        return [parts[i:i + 2] for i in range(0, len(parts), 2)]

    line = parse(line_coords, "计数线", 4)
    anchor = parse(anchor_coords, "内侧锚点", 2)
    roi = parse(roi_coords, "ROI")
    return validate_counting_geometry(line, anchor[0] if anchor else None, roi, require_line=require_line)
