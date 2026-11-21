"""Offline, bounded point-cloud update for Bridge R01. No mesh is generated."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from hole_diagnosis import quat_to_R, read_cameras_bin, read_images_bin


VERTEX = np.dtype([(n, '<f4') for n in ('x', 'y', 'z', 'nx', 'ny', 'nz')]
                  + [(n, 'u1') for n in ('red', 'green', 'blue')])


def cloud(path):
    with path.open('rb') as f:
        header = []
        for _ in range(100):
            line = f.readline().decode('ascii').strip()
            if line == 'end_header':
                break
            header.append(line)
        else:
            raise ValueError('Invalid PLY header')
        props = [s.split()[1:] for s in header if s.startswith('property ')]
        expected = [['float' if VERTEX[n].kind == 'f' else 'uchar', n] for n in VERTEX.names]
        if props != expected or 'format binary_little_endian 1.0' not in header:
            raise ValueError(f'Unexpected point-cloud layout: {path}')
        count, = [int(l.split()[-1]) for l in header if l.startswith('element vertex ')]
        a = np.fromfile(f, dtype=VERTEX, count=count)
        if len(a) != count or f.read(1):
            raise ValueError('Invalid point-cloud size')
        return a


def write_cloud(path, a):
    fields = '\n'.join(f'property {"float" if VERTEX[n].kind == "f" else "uchar"} {n}' for n in VERTEX.names)
    with path.open('xb') as f:
        f.write(f'ply\nformat binary_little_endian 1.0\nelement vertex {len(a)}\n{fields}\nend_header\n'.encode())
        a.tofile(f)


def xyz(a):
    return np.column_stack([a[n] for n in ('x', 'y', 'z')]).astype(float)


def inside(v, lo, hi):
    return ((v >= lo) & (v <= hi)).all(axis=1)


def depth(path):
    with path.open('rb') as f:
        header = bytearray()
        while header.count(b'&') < 3:
            ch = f.read(1)
            if not ch or len(header) >= 100:
                raise ValueError('Invalid depth-map header')
            header.extend(ch)
        w, h, channels = map(int, header.decode().split('&')[:3])
        if channels != 1:
            raise ValueError('Expected one-channel depth map')
        return np.fromfile(f, dtype='<f4').reshape(h, w)


def supported(points, image, camera, d, tolerance=0.012):
    if camera['model'] == 1:
        fx, fy, cx, cy = camera['params']
    elif camera['model'] == 2 and camera['params'][3] == 0:
        fx, cx, cy, _ = camera['params']
        fy = fx
    else:
        raise ValueError('Expected an undistorted camera')
    h, w = d.shape
    p = points @ quat_to_R(image['qvec']).T + image['tvec']
    z = p[:, 2]
    safe = np.where(z > 0, z, 1)
    u = np.floor((fx * p[:, 0] / safe + cx) * w / camera['width']).astype(int)
    v = np.floor((fy * p[:, 1] / safe + cy) * h / camera['height']).astype(int)
    ok = (z > 0) & (u >= 0) & (u < w) & (v >= 0) & (v < h)
    measured = d[v[ok], u[ok]]
    ok[ok] = np.isfinite(measured) & (measured > 0) & (abs(measured - z[ok]) <= tolerance * z[ok])
    return ok


def merge(parent, additions, lo, hi, *, near=0.025, distinct=0.004):
    a, b = xyz(parent), xyz(additions)
    sources = a[inside(a, lo, hi)]
    if not len(sources) or not len(b) or not np.isfinite(b).all():
        raise ValueError('Empty or invalid patch')
    distances = cKDTree(sources).query(b)[0]
    # Supplement observed surfaces only; far samples and near duplicates are rejected.
    accepted = inside(b, lo, hi) & (distances <= near) & (distances >= distinct)
    if not accepted.any():
        raise ValueError('No supported non-duplicate additions')
    return np.concatenate((parent, additions[accepted]))


def heldout_landmarks(runtime, names, lo, hi):
    """Use held-out observed sparse-track positions as a proxy, not dense truth."""
    sparse = runtime / 'txt'
    points = {}
    for line in (sparse / 'points3D.txt').read_text().splitlines():
        if line and not line.startswith('#'):
            cols = line.split()
            points[int(cols[0])] = list(map(float, cols[1:4]))
    lines = [line for line in (sparse / 'images.txt').read_text().splitlines()
             if not line.startswith('#')]
    result = {}
    for pose, observations in zip(lines[::2], lines[1::2]):
        name = pose.split()[-1]
        if name not in names:
            continue
        ids = {int(i) for i in observations.split()[2::3] if int(i) in points}
        positions = np.asarray([points[i] for i in sorted(ids)], dtype=float).reshape(-1, 3)
        result[name] = positions[inside(positions, lo, hi)]
    if set(result) != set(names) or not all(len(v) for v in result.values()):
        raise ValueError('No held-out landmarks in one or more views')
    return result


def landmark_metrics(points, landmarks, lo, hi):
    local = points[inside(points, lo, hi)]
    if not len(local):
        raise ValueError('No patch points to evaluate')
    tree = cKDTree(local)
    result = {}
    for name, stations in landmarks.items():
        d = tree.query(stations)[0]
        result[name] = dict(landmarks=len(d), median_distance=float(np.median(d)),
                            p90_distance=float(np.quantile(d, .9)), within_0_02=float(np.mean(d <= .02)))
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime', type=Path, required=True)
    parser.add_argument('--parent-cloud', type=Path, required=True)
    parser.add_argument('--parent-glb', type=Path, required=True)
    args = parser.parse_args()
    r = args.runtime
    cfg = json.loads((r / 'views.json').read_text())
    images = {i['name']: i for i in read_images_bin(r / 'seed/sparse/images.bin')}
    cameras = read_cameras_bin(r / 'seed/sparse/cameras.bin')
    lo, hi = np.array([6.45, -.78, 2.43]), np.array([7.98, -.35, 3.20])
    # Leave a 0.03-unit guard band unchanged.
    lo, hi = lo + .03, hi - .03
    digest = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    before = {p.name: digest(p) for p in (args.parent_cloud, args.parent_glb)}
    parent = cloud(args.parent_cloud)
    holdout = heldout_landmarks(r, cfg['holdout'], lo, hi)
    counts = {}
    for arm in ('control', 'candidate'):
        data = cloud(r / arm / 'fused.ply')
        local = data[inside(xyz(data), lo, hi)]
        points = xyz(local)
        if arm == 'candidate':
            votes = np.zeros(len(points), dtype=int)
            for name in cfg['new']:
                image = images[name]
                votes += supported(points, image, cameras[image['camera_id']],
                                   depth(r / arm / 'stereo/depth_maps' / (name+'.geometric.bin')))
            accepted = local[votes >= 2]
        else:
            accepted = local
        fused = merge(parent, accepted, lo, hi)
        if fused[:len(parent)].tobytes() != parent.tobytes() or not inside(xyz(fused[len(parent):]), lo, hi).all():
            raise AssertionError('Parent or outside patch changed')
        path = r / f'{arm}-merged.ply'
        write_cloud(path, fused)
        counts[arm] = {'local_points':len(local), 'eligible_points':len(accepted),
                       'added_points':len(fused)-len(parent), 'output_sha256':digest(path),
                       'holdout_sparse_landmark_proxy':landmark_metrics(xyz(fused), holdout, lo, hi)}
    assert before == {p.name: digest(p) for p in (args.parent_cloud, args.parent_glb)}
    (r / 'fusion-result.json').write_text(json.dumps({'parent':'18469/#24 R01; shares C01 filtered point cloud',
        'parent_sha256':before, 'child_geometry':'point cloud only', 'parent_points':len(parent),
        'core_bounds':[lo.tolist(), hi.tolist()], 'guard_band':0.03, 'arms':counts,
        'views':cfg, 'parent_holdout_sparse_landmark_proxy':landmark_metrics(xyz(parent), holdout, lo, hi),
        'new_support':'two new-view geometric depths within relative 1.2%; not held-out ground truth'}, indent=2))


if __name__ == '__main__':
    main()
