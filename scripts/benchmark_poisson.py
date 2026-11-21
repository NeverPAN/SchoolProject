"""Compare COLMAP Poisson thread counts on one unchanged fused PLY."""
import argparse
import hashlib
import json
import subprocess
import time
from pathlib import Path

import numpy as np
import trimesh
from scipy.spatial import cKDTree


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('colmap', type=Path)
    parser.add_argument('point_cloud', type=Path)
    parser.add_argument('output', type=Path)
    parser.add_argument('--timeout', type=int, default=90)
    args = parser.parse_args()
    args.output.mkdir(parents=True)
    digest = hashlib.sha256()
    with args.point_cloud.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    report = {'sha256': digest.hexdigest(), 'depth': 8, 'runs': []}
    meshes = {}
    for threads in (1, 4, -1):
        path = args.output / f'mesh-{threads}.ply'
        started = time.monotonic()
        with path.with_suffix('.log').open('wb') as log:
            try:
                result = subprocess.run([str(args.colmap), 'poisson_mesher', '--input_path', str(args.point_cloud),
                                         '--output_path', str(path), '--PoissonMeshing.depth', '8',
                                         '--PoissonMeshing.num_threads', str(threads)], stdout=log,
                                        stderr=subprocess.STDOUT, timeout=args.timeout, check=False)
                status = 'completed' if result.returncode == 0 else f'exit_{result.returncode}'
            except subprocess.TimeoutExpired:
                status = 'timeout'
        row = {'threads': threads, 'status': status, 'seconds': time.monotonic() - started}
        if status == 'completed':
            mesh = trimesh.load_mesh(path, process=False)
            meshes[threads] = mesh
            row.update(vertices=len(mesh.vertices), faces=len(mesh.faces), surface_area=float(mesh.area))
        report['runs'].append(row)
    if 1 in meshes and 4 in meshes:
        report['bidirectional_vertex_p95'] = [float(np.quantile(cKDTree(meshes[b].vertices).query(meshes[a].vertices)[0], .95))
                                              for a, b in [(1, 4), (4, 1)]]
    (args.output / 'benchmark.json').write_text(json.dumps(report, indent=2) + '\n')


if __name__ == '__main__':
    main()
