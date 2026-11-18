"""Estimate a display up direction from COLMAP camera rolls, not gravity."""
from __future__ import annotations

import struct
from pathlib import Path

import numpy as np


def estimate_display_up(images_bin: Path) -> list[float] | None:
    """Return a hint only when enough registered camera ups agree."""
    try:
        size = images_bin.stat().st_size
        directions = []
        with images_bin.open('rb') as f:
            count, = struct.unpack('<Q', f.read(8))
            if not 3 <= count <= size // 73:
                return None
            for _ in range(count):
                _, w, x, y, z, *_ = struct.unpack('<i7di', f.read(64))
                # COLMAP stores world-to-camera pose. Image up is local -Y.
                q = np.array([w, x, y, z])
                norm = np.linalg.norm(q)
                if np.isfinite(norm) and norm > 1e-8:
                    w, x, y, z = q / norm
                    directions.append([-2 * (x*y + w*z), -1 + 2 * (x*x + z*z), -2 * (y*z - w*x)])
                while (ch := f.read(1)) != b'\0':
                    if not ch:
                        return None
                points, = struct.unpack('<Q', f.read(8))
                if points > (size - f.tell()) // 24:
                    return None
                f.seek(points * 24, 1)
        if len(directions) < max(3, count * 0.8):
            return None
        scores = np.asarray(directions)
        up = np.median(scores, axis=0)
        norm = np.linalg.norm(up)
        if norm < 0.5:
            return None
        up /= norm
        if np.mean(scores @ up >= np.cos(np.deg2rad(35))) < 0.8:
            return None
        return up.tolist()
    except (OSError, struct.error):
        return None
