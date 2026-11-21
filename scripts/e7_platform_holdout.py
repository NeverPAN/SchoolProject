"""Score the published Bridge #28 PLY against the frozen E7 depth evidence (CPU only)."""
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

import numpy as np
from scipy.spatial import cKDTree

from e7_holdout_depth import (PLAN, camera_rows, cfg_views, distance_summary, extrinsics,
                              inside, load_json, project_with_splat, projection_summary,
                              read_depth, read_poses, sha, verified_points)
from local_bridge_fusion import cloud, xyz

EVIDENCE = Path(__file__).resolve().parents[1] / 'docs/e7-bridge-evidence'


def require(condition, message):
    if not condition:
        raise ValueError(message)


def score(args):
    frozen_path = EVIDENCE / 'holdout-depth.json'
    frozen = load_json(frozen_path)
    platform = load_json(EVIDENCE / 'platform-local-api.json')
    job = load_json(args.child.with_name('job_manifest.json'))
    require(frozen['dense_support']['passed'] and frozen['predeclared'] == PLAN,
            'Missing frozen scoring protocol or depth support')
    require(job['job_id'] == 15 and job['parent_model_id'] == 24 and job['pipeline_mode'] == 'local_reconstruct'
            and sha(args.child) == job['output_sha256'] == platform['download_sha256']['28']
            and job['parent_model_sha256'] == platform['download_sha256']['24'], 'Unexpected platform output')
    paths = {arm: Path(meta['path']) for arm, meta in frozen['scored_clouds'].items()}
    hashes = {arm: sha(path) for arm, path in paths.items()}
    require(hashes == {arm: meta['sha256'] for arm, meta in frozen['scored_clouds'].items()}
            and hashes['parent'] == job['parent_cloud_sha256'], 'Cloud provenance mismatch')
    graph = frozen['mvs']['reference_and_source_graph']
    names = set(graph) | {name for sources in graph.values() for name in sources}
    holdout_hashes = {name: sha(args.work / 'holdout-images' / name) for name in names}
    require(holdout_hashes == frozen['mvs']['photo_sha256'], 'Held-out photo hashes differ')
    train = job['view_sha256']
    log = args.job_log.read_text()
    refs = re.findall(r'Processing view \d+ / 14 for (\S+)', log)
    require(set(refs) == set(train) and len(refs) == 28 and len(train) == 14,
            'Local PatchMatch references do not match the published 14-image manifest')
    # The local worker writes explicit all-other-selected-image sources for each reference.
    ref_indices = re.findall(r'ref_image_idx: (\d+)', log)
    source_indices = re.findall(r'patch_match\.cc:(?:58|60)\] (\d+)\s*$', log, re.M)
    require(len(ref_indices) == 28 and len(set(ref_indices)) == 14
            and len(source_indices) == 28 * 13 and set(source_indices) == set(ref_indices),
            'Local PatchMatch source indices do not match the logged reference index pool')
    require(not names.intersection(train) and not set(holdout_hashes.values()).intersection(train.values()),
            'Held-out photos overlap #28 training references or source pool')
    parent_evidence = frozen['exclusions']['parent_dense_verified']
    parent_dense = Path(parent_evidence['path'])
    parent_cfg = parent_dense / 'stereo/patch-match.cfg'
    parent_refs, parent_sources = cfg_views(parent_cfg)
    inventory = {p.name for p in (parent_dense / 'images').iterdir()}
    originals = {row.split()[0] for row in (EVIDENCE.parent / 'phase2-evidence/bridge100.sha256').read_text().splitlines()}
    require(sha(parent_cfg) == parent_evidence['config_sha256']
            and parent_refs == inventory and len(parent_refs) == parent_evidence['reference_count']
            and len(originals) == len(inventory)
            and not names.intersection(parent_refs | parent_sources | inventory)
            and not set(holdout_hashes.values()).intersection(originals), 'Parent dense provenance or exclusion failed')
    cfg = args.work / 'dense/stereo/patch-match.cfg'
    require(sha(cfg) == frozen['mvs']['patchmatch_config_sha256'], 'Held-out graph hash mismatch')
    camera_dir = args.work / 'dense-camera-txt'
    cameras = camera_rows(camera_dir / 'cameras.txt')
    poses = read_poses(camera_dir / 'images.txt')
    depth_dir = args.work / 'dense/stereo/depth_maps'
    depths, models = {}, {}
    for name in graph:
        path = depth_dir / (name + '.geometric.bin')
        require(sha(path) == frozen['mvs']['geometric_depth_sha256'][name], f'Depth map changed: {name}')
        depths[name] = read_depth(path)
        r, t = extrinsics(poses[name][0])
        w, h, fx, fy, cx, cy = cameras[poses[name][0].split()[-2]]
        sy, sx = np.asarray(depths[name].shape) / [h, w]
        models[name] = (r, t, np.array([fx * sx, fy * sy]), np.array([cx * sx, cy * sy]))
    bounds = np.array(frozen['roi_core_bounds_native'])
    params, counts = job['resolved_params'], job['counts']
    core = np.asarray([params['core_min'], params['core_max']])
    parent, child = cloud(paths['parent']), cloud(args.child)
    n = counts['parent_points']
    require(len(parent) == n and len(child) - n == counts['added_points'] == 543
            and np.array_equal(parent.view('u1'), child[:n].view('u1')), 'Parent vertex-record prefix changed')
    additions = xyz(child[n:])
    require(np.all(core[0] >= bounds[0]) and np.all(core[1] <= bounds[1])
            and inside(additions, core).all(), 'Updated points outside guarded core')
    parents = xyz(parent)
    parent_roi = parents[inside(parents, bounds)]
    prior = {arm: xyz(cloud(paths[arm])) for arm in ('control', 'candidate')}
    points = {'parent': parent_roi, 'platform_28': np.concatenate((parent_roi, additions)),
              **{arm: v[inside(v, bounds)] for arm, v in prior.items()}}
    trees = {arm: cKDTree(points[arm]) for arm in ('parent', 'platform_28')}
    scores, references = {}, []
    for name in frozen['required_holdouts']:
        verified, pixels, support = verified_points(name, graph, depths, models, bounds)
        old = frozen['scores'][name]
        require(support['passed'] and len(verified) == frozen['dense_support']['by_view'][name]['verified_roi_depth_pixels'],
                f'Frozen support changed: {name}')
        references.append(verified)
        _, i = np.unique(np.floor((verified - bounds[0]) / PLAN['score']['holdout_voxel_model_units']),
                         axis=0, return_index=True)
        require(len(i) == old['holdout_sample_voxels'], f'Frozen voxel count changed: {name}')
        surfaces = {arm: project_with_splat(v, models[name], depths[name].shape) for arm, v in points.items()}
        u, v = pixels.T
        common = np.logical_and.reduce([np.isfinite(surfaces[arm][v, u]) for arm in ('parent', 'control', 'candidate')])
        require(int(common.sum()) == old['arms']['parent']['holdout_ray_projection']['common_pixel_count']
                and np.isfinite(surfaces['platform_28'][v[common], u[common]]).all(), 'Frozen common-ray mask changed')
        scores[name] = {'verified_pixels': len(verified), 'holdout_sample_voxels': len(i),
                        'arms': {arm: {'roi_cloud_points': len(points[arm]),
                                       'holdout_to_cloud': distance_summary(tree.query(verified[i])[0]),
                                       'holdout_ray_projection': projection_summary(surfaces[arm], depths[name], pixels, common)}
                                 for arm, tree in trees.items()}}
        for field in ('holdout_to_cloud', 'holdout_ray_projection'):
            for key, value in old['arms']['parent'][field].items():
                actual = scores[name]['arms']['parent'][field][key]
                require(all(np.isclose(actual[k], v, rtol=0, atol=1e-7) for k, v in value.items())
                        if isinstance(value, dict) else np.isclose(actual, value, rtol=0, atol=1e-7),
                        f'Frozen parent score differs: {name} {field}/{key}')
    return {'artifact': '18479 project 1 / model 28 / job 15', 'parent_model': 24,
            'sha256': {'frozen_report': sha(frozen_path), 'platform_28': sha(args.child), 'baseline_clouds': hashes,
                       'job_manifest': sha(args.child.with_name('job_manifest.json')),
                       'job_dense_log': sha(args.job_log), 'parent_patchmatch': sha(parent_cfg),
                       'holdout_patchmatch': sha(cfg), 'holdout_photos': holdout_hashes,
                       'holdout_camera_text': {p: sha(camera_dir / p) for p in ('cameras.txt', 'images.txt')},
                       'holdout_depths': frozen['mvs']['geometric_depth_sha256']},
            'holdout_graph': graph, 'local_training': train, 'local_source_policy': 'explicit all other selected views',
            'logged_reference_indices': sorted(map(int, set(ref_indices))),
            'logged_source_indices': sorted(map(int, set(source_indices))),
            'parent_training_inventory': len(inventory), 'holdout_dense_training_overlap': False,
            'parent_vertex_records_identical': True, 'parent_points': n, 'added_points': len(additions),
            'all_additions_inside_guarded_core': True, 'platform_core_bounds_native': core.tolist(),
            'evaluation_bounds_native': bounds.tolist(), 'ray_mask': 'frozen parent/L00/L01 common rays',
            'scores': scores, 'addition_to_any_verified_depth': distance_summary(cKDTree(np.concatenate(references)).query(additions)[0]),
            'limits': ['Shared sparse calibration; held out from dense inputs only.',
                       'Native model units, not meters; no exterior roof, mesh seams or texture evaluated.',
                       'Add-only nearest-sample distances cannot worsen; common-ray depth can worsen.',
                       'No new GPU run, mesh stitch, or publication.']}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('work', 'child', 'job-log', 'output'):
        parser.add_argument('--' + name, required=True, type=Path)
    args = parser.parse_args()
    report = score(args)
    with args.output.open('x', encoding='utf-8') as output:
        json.dump(report, output, ensure_ascii=False, indent=2, allow_nan=False)
        output.write('\n')


if __name__ == '__main__':
    main()
