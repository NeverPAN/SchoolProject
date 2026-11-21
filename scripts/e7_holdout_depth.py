"""Offline Bridge E7 holdout-only depth evaluation; never writes reconstruction inputs.

Run preflight, then prepare only if it passes. Before launching the printed COLMAP
command, coordinate GPU availability separately. Run score after PatchMatch exits.
Dependencies: NumPy, SciPy, CUDA-enabled COLMAP 3.13 for prepare / PatchMatch.
"""
from __future__ import annotations

import argparse
import hashlib
from itertools import combinations, product
import json
from pathlib import Path
import shutil
import subprocess

import numpy as np
from scipy.spatial import cKDTree
from scipy.spatial.transform import Rotation

PLAN = {
    'preflight': {'min_shared_roi_tracks': 12, 'min_occupied_cells': 4,
                  'median_triangulation_angle_deg': [2, 35]},
    'dense_gate': {'min_supported_pixels_per_required_view': 500,
                   'min_cells_per_required_view': 6, 'min_pixels_per_counted_cell': 20,
                   'max_relative_source_z_residual': 0.01, 'max_roundtrip_error_px': 1.0},
    'roi_grid': '4 x 4 equal world-coordinate cells on core X/Z; clamp upper boundary',
    'score': {'distance_tolerance_model_units': 0.02, 'holdout_voxel_model_units': 0.005,
              'projection': 'core points only; nearest camera-Z (z-buffer) with a 1-pixel Chebyshev splat',
              'additions_metric': 'added core points to required-view union of verified holdout samples; partially visible ROI only'},
}


def sha(path):
    digest = hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def load_json(path):
    return json.loads(path.read_text())


def write_json(path, obj):
    path.write_text(json.dumps(obj, indent=2, allow_nan=False) + '\n')


def read_poses(path):
    lines = [line for line in path.read_text().splitlines() if not line.startswith('#')]
    return {pose.split()[-1]: (pose, obs) for pose, obs in zip(lines[::2], lines[1::2])}


def extrinsics(pose):
    _, qw, qx, qy, qz, tx, ty, tz, *_ = pose.split()
    r = Rotation.from_quat([float(v) for v in (qx, qy, qz, qw)]).as_matrix()
    t = np.array([float(v) for v in (tx, ty, tz)])
    return r, t


def inside(points, bounds):
    return ((points >= bounds[0]) & (points <= bounds[1])).all(axis=1)


def grid(points, bounds):
    lo, hi = np.asarray(bounds)[:, [0, 2]]
    cells = np.clip(np.floor(4 * (points[:, [0, 2]] - lo) / (hi - lo)).astype(int), 0, 3)
    return np.bincount(cells[:, 0] * 4 + cells[:, 1], minlength=16).reshape(4, 4)


def cfg_views(path):
    refs, sources = set(), set()
    lines = [s.strip() for s in path.read_text().splitlines() if s.strip() and not s.startswith('#')]
    if len(lines) % 2:
        raise ValueError(f'Invalid PatchMatch config: {path}')
    for ref, src in zip(lines[::2], lines[1::2]):
        refs.add(ref)
        if not src.startswith('__auto__'):
            sources.update(s.strip() for s in src.split(','))
    return refs, sources


def preflight(args):
    runtime = args.retained
    prior = load_json(args.prior)
    bounds = np.asarray(prior['core_bounds'])
    views = load_json(runtime / 'views.json')
    required = sorted(views['holdout'])
    poses = read_poses(runtime / 'txt/images.txt')
    # All original registration inputs are conservatively blocked, not just those
    # explicitly marked as local PatchMatch references.
    blocked = {p.name for p in (runtime / 'images').glob('img_*.jpg')}
    if len(blocked) != prior['registration']['base_images']:
        raise ValueError('Original training image manifest count mismatch')
    exclusion = {'parent_original_training_count': len(blocked)}
    if args.parent_dense is not None:
        config = args.parent_dense / 'stereo/patch-match.cfg'
        refs, srcs = cfg_views(config)
        inventory = {p.name for p in (args.parent_dense / 'images').iterdir()}
        if refs != blocked or inventory != blocked:
            raise ValueError('Parent refs / source inventory mismatch')
        blocked |= srcs
        exclusion['parent_dense_verified'] = {
            'path': str(args.parent_dense), 'config_sha256': sha(config),
            'reference_count': len(refs), 'source_candidate_count': len(inventory),
            'automatic_sources': '__auto__' in config.read_text(),
        }
        if args.parent is not None:
            digest = sha(args.parent)
            if digest != prior['parent_sha256']['fused-filtered.ply']:
                raise ValueError('Parent cloud SHA-256 mismatch')
            exclusion['parent_dense_verified']['matched_parent_cloud_sha256'] = digest
    for arm in ('control', 'candidate'):
        config = runtime / arm / 'stereo/patch-match.cfg'
        refs, srcs = cfg_views(config)
        all_images = {p.name for p in (runtime / arm / 'images').iterdir()}
        blocked |= refs | srcs | all_images
        exclusion[arm] = {'config_sha256': sha(config), 'reference_names': sorted(refs),
                          'explicit_source_names': sorted(srcs)}
    eligible = sorted(name for name in poses if name not in blocked and (runtime / 'images' / name).is_file())
    if not set(required) <= set(eligible):
        raise ValueError('Missing required holdout or training overlap')
    roi_xyz = {}
    for row in (runtime / 'txt/points3D.txt').read_text().splitlines():
        if row and not row.startswith('#'):
            pid, x, y, z, *_ = row.split(maxsplit=4)
            xyz = np.array([float(x), float(y), float(z)])
            if inside(xyz[None, :], bounds)[0]:
                roi_xyz[int(pid)] = xyz
    tracks, centers = {}, {}
    for name in eligible:
        pose, obs = poses[name]
        tracks[name] = {int(pid) for pid in obs.split()[2::3]} & roi_xyz.keys()
        r, t = extrinsics(pose)
        centers[name] = -r.T @ t
    pairs = []
    edges = {name: set() for name in eligible}
    pf = PLAN['preflight']
    for a, b in combinations(eligible, 2):
        points = np.array([roi_xyz[k] for k in sorted(tracks[a] & tracks[b])]).reshape(-1, 3)
        u, v = points - centers[a], points - centers[b]
        angle = np.degrees(np.arccos(np.clip(np.sum(u * v, axis=1) /
                           (np.linalg.norm(u, axis=1) * np.linalg.norm(v, axis=1)), -1, 1)))
        median = float(np.median(angle)) if len(points) else None
        cells = int(np.count_nonzero(grid(points, bounds)))
        pass_pair = (len(points) >= pf['min_shared_roi_tracks'] and cells >= pf['min_occupied_cells']
                     and pf['median_triangulation_angle_deg'][0] <= median <= pf['median_triangulation_angle_deg'][1])
        pairs.append({'views': [a, b], 'shared_roi_tracks': len(points), 'occupied_roi_cells': cells,
                      'median_triangulation_angle_deg': median, 'pass': pass_pair})
        if pass_pair:
            edges[a].add(b)
            edges[b].add(a)
    # Optional views must directly support at least one required holdout in the ROI.
    selected = set(required) | {b for a in required for b in edges[a]}
    graph = {a: sorted(edges[a] & selected) for a in sorted(selected)}
    preflight_pass = all(graph[a] for a in required)
    result = {
        'status': 'preflight_pass' if preflight_pass else 'insufficient_overlap',
        'retained_experiment': str(runtime), 'isolated_workspace': str(args.work),
        'roi_core_bounds_native': bounds.tolist(), 'parent': prior['parent'],
        'predeclared': PLAN, 'required_holdouts': required, 'exclusions': exclusion,
        'blocked_unique_view_count': len(blocked), 'eligible_registered_unused': eligible,
        'registered_pose_file_sha256': sha(runtime / 'txt/images.txt'),
        'registered_tracks_file_sha256': sha(runtime / 'txt/points3D.txt'),
        'preflight': {'passed': preflight_pass, 'roi_track_count_by_view': {a: len(tracks[a]) for a in eligible},
                      'pairs': pairs, 'pair_pass_source_graph': graph},
        'holdout_only_dense_scoring_performed': False, 'independent_depth_available': False,
        'stitch_attempted': False, 'scores': None,
        'limits': ['Registration/calibration reuse the parent sparse frame and matched landmarks; '
                   'the preflight tracks are not independently measured geometry.',
                   'Only holdout photographs can be dense inputs. Dense self-consistency, if measured, '
                   'is not external ground truth or a test of the roof exterior, seam, or whole model.'],
    }
    write_json(args.work / 'result.json', result)


def prepare(args):
    result = load_json(args.work / 'result.json')
    if result['status'] != 'preflight_pass':
        raise ValueError('GPU preparation requires passing preflight')
    graph = result['preflight']['pair_pass_source_graph']
    poses = read_poses(args.retained / 'txt/images.txt')
    images, sparse, dense = [args.work / sub for sub in ('holdout-images', 'pose-only', 'dense')]
    for folder in (images, sparse, dense):
        folder.mkdir()
    corners = np.array(list(product(*np.asarray(result['roi_core_bounds_native']).T)))
    depths = []
    hashes = {}
    for name in graph:
        source = args.retained / 'images' / name
        shutil.copyfile(source, images / name)
        hashes[name] = sha(images / name)
        r, t = extrinsics(poses[name][0])
        depths.extend((corners @ r.T + t)[:, 2])
    depth_min, depth_max = min(depths) * 0.5, max(depths) * 1.5
    if not 0 < depth_min < depth_max:
        raise ValueError('ROI corner search depth is not positive in all selected cameras')
    shutil.copyfile(args.retained / 'txt/cameras.txt', sparse / 'cameras.txt')
    (sparse / 'images.txt').write_text(''.join(poses[name][0] + '\n\n' for name in graph))
    (sparse / 'points3D.txt').touch()
    undistort = [args.colmap, 'image_undistorter', '--image_path', str(images), '--input_path', str(sparse),
                 '--output_path', str(dense), '--max_image_size', '1600']
    with (args.work / 'undistort.log').open('w') as log:
        subprocess.run(undistort, stdout=log, stderr=subprocess.STDOUT, check=True)
    sparse_txt = args.work / 'dense-camera-txt'
    sparse_txt.mkdir()
    subprocess.run([args.colmap, 'model_converter', '--input_path', str(dense / 'sparse'),
                    '--output_path', str(sparse_txt), '--output_type', 'TXT'], check=True)
    if set(read_poses(sparse_txt / 'images.txt')) != set(graph):
        raise ValueError('Undistorted pose inventory does not equal holdout graph')
    if set(p.name for p in (dense / 'images').iterdir()) != set(graph):
        raise ValueError('Undistorted photo inventory does not equal holdout graph')
    cfg = dense / 'stereo/patch-match.cfg'
    cfg.write_text(''.join(name + '\n' + ', '.join(srcs) + '\n' for name, srcs in graph.items()))
    (dense / 'stereo/fusion.cfg').write_text(''.join(name + '\n' for name in graph))
    patches = [args.colmap, 'patch_match_stereo', '--workspace_path', str(dense),
               '--PatchMatchStereo.gpu_index', '0,1', '--PatchMatchStereo.max_image_size', '1600',
               '--PatchMatchStereo.num_iterations', '5', '--PatchMatchStereo.geom_consistency', '1',
               '--PatchMatchStereo.filter_min_num_consistent', '1',
               '--PatchMatchStereo.depth_min', str(depth_min), '--PatchMatchStereo.depth_max', str(depth_max)]
    result['mvs'] = {'status': 'prepared_not_run', 'colmap_version': subprocess.check_output(
        [args.colmap, '-h'], text=True).splitlines()[:2], 'undistort_argv': undistort,
        'patchmatch_argv': patches, 'reference_and_source_graph': graph, 'photo_sha256': hashes,
        'patchmatch_config_sha256': sha(cfg), 'sparse_point_count': 0, 'reused_depth_map_count': 0,
        'depth_range': 'ROI camera-Z corner extrema expanded by factors 0.5 and 1.5; no sparse depth priors'}
    write_json(args.work / 'result.json', result)


def read_depth(path):
    with path.open('rb') as f:
        header = bytearray()
        while header.count(b'&') < 3:
            char = f.read(1)
            if not char or len(header) > 100:
                raise ValueError(f'Invalid depth header: {path}')
            header.extend(char)
        w, h, c = map(int, header.decode().split('&')[:3])
        if c != 1:
            raise ValueError('Expected single-channel depth')
        return np.fromfile(f, '<f4').reshape(h, w)


def camera_rows(path):
    cams = {}
    for row in path.read_text().splitlines():
        if not row or row.startswith('#'):
            continue
        idx, model, w, h, *params = row.split()
        p = list(map(float, params))
        if model == 'PINHOLE':
            fx, fy, cx, cy = p
        elif model == 'SIMPLE_PINHOLE' or (model == 'SIMPLE_RADIAL' and p[3] == 0):
            fx, cx, cy = p[:3]
            fy = fx
        else:
            raise ValueError(f'Unexpected undistorted camera {model}')
        cams[idx] = (int(w), int(h), fx, fy, cx, cy)
    return cams


def project(points, model):
    r, t, f, c = model
    p = points @ r.T + t
    z = p[:, 2]
    return p[:, :2] / np.where(z[:, None] > 0, z[:, None], 1) * f + c, z


def backproject(pixels, z, model):
    r, t, f, c = model
    p = np.column_stack(((pixels - c) / f * z[:, None], z))
    return (p - t) @ r


def verified_points(name, graph, depths, models, bounds):
    d = depths[name]
    y, x = np.nonzero(np.isfinite(d) & (d > 0))
    pixels = np.column_stack([x + .5, y + .5])
    model = models[name]
    points = backproject(pixels, d[y, x], model)
    mask = inside(points, bounds)
    points, pixels = points[mask], pixels[mask]
    votes = np.zeros(len(points), dtype=int)
    tally = {}
    gate = PLAN['dense_gate']
    for other in graph[name]:
        od = depths[other]
        uv, oz = project(points, models[other])
        ix, iy = np.floor(uv).astype(int).T
        ok = (oz > 0) & (ix >= 0) & (iy >= 0) & (ix < od.shape[1]) & (iy < od.shape[0])
        inds = np.flatnonzero(ok)
        z = od[iy[inds], ix[inds]]
        close = np.isfinite(z) & (z > 0) & (abs(z - oz[inds]) <= gate['max_relative_source_z_residual'] * oz[inds])
        inds, z = inds[close], z[close]
        # Sample the source's nearest pixel depth on the continuous projected ray,
        # then require that depth to reproject back to the original reference ray.
        roundtrip, rz = project(backproject(uv[inds], z, models[other]), model)
        good = inds[(rz > 0) & (np.linalg.norm(roundtrip - pixels[inds], axis=1) <= gate['max_roundtrip_error_px'])]
        votes[good] += 1
        tally[other] = len(good)
    verified = points[votes > 0]
    cells = grid(verified, bounds)
    counted = int(np.count_nonzero(cells >= gate['min_pixels_per_counted_cell']))
    stats = {'map_pixels': int(d.size), 'valid_geometric_depth_pixels': len(x), 'valid_roi_depth_pixels': len(points),
             'verified_roi_depth_pixels': len(verified), 'verified_pixels_by_source': tally,
             'roi_grid_supported_pixel_counts': cells.tolist(), 'counted_grid_cells': counted,
             'passed': (len(verified) >= gate['min_supported_pixels_per_required_view']
                        and counted >= gate['min_cells_per_required_view'])}
    return verified, pixels[votes > 0].astype(int), stats


def project_with_splat(points, model, shape):
    uv, z = project(points, model)
    x, y = np.floor(uv).astype(int).T
    h, w = shape
    surface = np.full(shape, np.inf)
    for dx, dy in product((-1, 0, 1), repeat=2):
        xx, yy = x + dx, y + dy
        mask = (z > 0) & (xx >= 0) & (yy >= 0) & (xx < w) & (yy < h)
        np.minimum.at(surface, (yy[mask], xx[mask]), z[mask])
    return surface


def projection_summary(surface, d, pixels, common):
    x, y = pixels.T
    rendered, observed = surface[y, x], d[y, x]
    valid = np.isfinite(rendered)
    residual = abs(rendered - observed)
    return {'covered_pixels': int(valid.sum()), 'verified_pixel_coverage': float(valid.mean()),
            'common_pixel_count': int(common.sum()),
            'common_absolute_z_error': distance_summary(residual[common]) if common.any() else None}


def read_cloud_xyz(path):
    dtype = np.dtype([(n, '<f4') for n in ('x', 'y', 'z', 'nx', 'ny', 'nz')]
                     + [(n, 'u1') for n in ('red', 'green', 'blue')])
    with path.open('rb') as f:
        header = []
        for _ in range(100):
            line = f.readline().decode('ascii').strip()
            if line == 'end_header':
                break
            header.append(line)
        else:
            raise ValueError('Invalid PLY header')
        count, = [int(row.split()[-1]) for row in header if row.startswith('element vertex ')]
        props = [row.split()[1:] for row in header if row.startswith('property ')]
        if ('format binary_little_endian 1.0' not in header
                or props != [['float' if dtype[n].kind == 'f' else 'uchar', n] for n in dtype.names]):
            raise ValueError('Unexpected cloud layout')
        a = np.fromfile(f, dtype, count)
        if len(a) != count or f.read(1):
            raise ValueError('Cloud data length mismatch')
    return np.column_stack([a[k] for k in ('x', 'y', 'z')])


def distance_summary(d):
    return {'median': float(np.median(d)), 'p90': float(np.quantile(d, .9)),
            'fraction_within_0_02': float(np.mean(d <= PLAN['score']['distance_tolerance_model_units']))}


def score(args):
    result = load_json(args.work / 'result.json')
    graph = result['mvs']['reference_and_source_graph']
    bounds = np.array(result['roi_core_bounds_native'])
    camera_txt = args.work / 'dense-camera-txt'
    cams, poses = camera_rows(camera_txt / 'cameras.txt'), read_poses(camera_txt / 'images.txt')
    depth_dir = args.work / 'dense/stereo/depth_maps'
    files = {name: depth_dir / (name + '.geometric.bin') for name in graph}
    if set(files.values()) != set(depth_dir.glob('*.geometric.bin')):
        raise ValueError('Generated geometric depth inventory differs from holdout graph')
    depths, models = {}, {}
    for name, path in files.items():
        d = read_depth(path)
        r, t = extrinsics(poses[name][0])
        w, h, fx, fy, cx, cy = cams[poses[name][0].split()[-2]]
        sy, sx = np.array(d.shape) / [h, w]
        depths[name] = d
        models[name] = (r, t, np.array([fx * sx, fy * sy]), np.array([cx * sx, cy * sy]))
    result['mvs']['status'] = 'maps_generated'
    result['mvs']['geometric_depth_sha256'] = {name: sha(path) for name, path in files.items()}
    verified, pixels, stats = {}, {}, {}
    for name in graph:
        verified[name], pixels[name], stats[name] = verified_points(name, graph, depths, models, bounds)
    passed = all(stats[name]['passed'] for name in result['required_holdouts'])
    result['dense_support'] = {'passed': passed, 'by_view': stats}
    result['status'] = 'scored' if passed else 'insufficient_dense_support'
    result['independent_depth_available'] = passed
    if passed:
        prior = load_json(args.prior)
        clouds = {'parent': args.parent, **{arm: args.retained / f'{arm}-merged.ply' for arm in ('control', 'candidate')}}
        expected = {'parent': prior['parent_sha256']['fused-filtered.ply'],
                    **{arm: prior['arms'][arm]['output_sha256'] for arm in ('control', 'candidate')}}
        paths = {arm: str(path) for arm, path in clouds.items()}
        actual = {arm: sha(path) for arm, path in clouds.items()}
        if actual != expected:
            raise ValueError('Unexpected baseline point-cloud content')
        result['scored_clouds'] = {arm: {'path': paths[arm], 'sha256': actual[arm]} for arm in paths}
        roi_clouds, additions = {}, {}
        for arm, path in clouds.items():
            xyz = read_cloud_xyz(path)
            in_roi = inside(xyz, bounds)
            roi_clouds[arm] = xyz[in_roi]
            if arm == 'parent':
                parent_count = len(xyz)
            else:
                additions[arm] = xyz[parent_count:][in_roi[parent_count:]]
        trees = {arm: cKDTree(points) for arm, points in roi_clouds.items()}
        holdout_tree = cKDTree(np.concatenate([verified[n] for n in result['required_holdouts']]))
        result['added_points_to_holdout'] = {arm: {'added_roi_points': len(points),
            **distance_summary(holdout_tree.query(points)[0])} for arm, points in additions.items()}
        result['scores'] = {}
        for name in result['required_holdouts']:
            points = verified[name]
            _, idx = np.unique(np.floor((points - bounds[0]) / PLAN['score']['holdout_voxel_model_units']),
                               return_index=True, axis=0)
            sample = points[idx]
            surfaces = {arm: project_with_splat(pts, models[name], depths[name].shape)
                        for arm, pts in roi_clouds.items()}
            x, y = pixels[name].T
            common = np.logical_and.reduce([np.isfinite(s[y, x]) for s in surfaces.values()])
            result['scores'][name] = {'holdout_sample_voxels': len(sample), 'arms': {
                arm: {'roi_cloud_points': len(roi_clouds[arm]),
                      'holdout_to_cloud': distance_summary(tree.query(sample)[0]),
                      'holdout_ray_projection': projection_summary(surfaces[arm], depths[name], pixels[name], common)}
                for arm, tree in trees.items()}}
        result['holdout_only_dense_scoring_performed'] = True
        result['limits'].append('Holdout-to-cloud nearest distance tests coverage and cannot worsen under '
                                'add-only fusion. The reverse distance on additions can penalize unobserved '
                                'or occluded surfaces. Point splats are not a full scene visibility test.')
    else:
        result['limits'].append('Geometric depth maps were generated but did not satisfy the predeclared ROI '
                                'support gate; no independent dense ROI accuracy comparison is reported.')
    write_json(args.work / 'result.json', result)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('stage', choices=['preflight', 'prepare', 'score'])
    for name in ('work', 'retained', 'prior'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--colmap', default='colmap')
    parser.add_argument('--parent', type=Path)
    parser.add_argument('--parent-dense', type=Path)
    args = parser.parse_args()
    if args.stage == 'score' and args.parent is None:
        parser.error('score requires --parent')
    globals()[args.stage](args)


if __name__ == '__main__':
    main()
