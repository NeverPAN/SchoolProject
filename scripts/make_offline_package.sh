#!/usr/bin/env bash
# 离线演示包打包脚本：仓库快照 + 关键模型 GLB + 前端构建 + 启动文档 + 校验和。
#
# 用法：
#   scripts/make_offline_package.sh --out <输出目录> \
#     [--model <GLB文件> ...] [--dist <frontend/dist 目录>] [--docs <额外文件> ...]
#
# 产物：<输出目录>/recon-offline-<日期>/
#   repo.tar.gz          git archive 干净快照（不含未跟踪与数据文件）
#   models/              关键演示模型（GLB，路径去重后拷贝）
#   frontend-dist.tar.gz 可选：生产构建（浏览器直接 serve 无需 node）
#   docs-copy/           答辩必读文档副本（总览/启动说明/演示流程/部署/报告/PPT）
#   MANIFEST.md          包清单（git head、包内容、复现说明）
#   SHA256SUMS           全部文件校验和（交接时用独立渠道核对）
#
# 打包内容只含仓库代码与明确指定的模型/文档；真实数据库、.env、demo 凭据
# （.demo-private/）永不出包。
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OUT="" DIST=""
MODELS=() DOCS=()

while [[ $# -gt 0 ]]; do
  case "$1" in
    --out) OUT="$2"; shift 2;;
    --dist) DIST="$(realpath "$2")"; shift 2;;
    --model) MODELS+=("$(realpath "$2")"); shift 2;;
    --docs) DOCS+=("$(realpath "$2")"); shift 2;;
    *) echo "未知参数: $1" >&2; exit 2;;
  esac
done
[[ -n "$OUT" ]] || { sed -n '2,18p' "${BASH_SOURCE[0]}" >&2; exit 2; }

HEAD="$(git -C "$REPO" rev-parse --short HEAD)"
PKG="$OUT/recon-offline-$(date +%Y%m%d)"
mkdir -p "$PKG/models" "$PKG/docs-copy"

# 1) 仓库干净快照
git -C "$REPO" archive --format=tar.gz -o "$PKG/repo.tar.gz" HEAD

# 2) 模型（去重拷贝，文件名冲突时加序号）
declare -A seen=()
for m in "${MODELS[@]:-}"; do
  [[ -n "$m" && -f "$m" ]] || { echo "跳过缺失模型: $m" >&2; continue; }
  name="$(basename "$m")"
  if [[ -n "${seen[$name]:-}" ]]; then
    name="$(basename "${m%.glb}")-$(basename "$(dirname "$m")").glb"
  fi
  cp "$m" "$PKG/models/$name"
  seen[$name]=1
done

# 3) 前端构建（可选）
if [[ -n "$DIST" ]]; then
  [[ -d "$DIST" ]] || { echo "dist 目录不存在: $DIST" >&2; exit 1; }
  tar -czf "$PKG/frontend-dist.tar.gz" -C "$(dirname "$DIST")" "$(basename "$DIST")"
fi

# 4) 答辩必读文档
for d in docs/项目总览.md docs/目录.md docs/三个演示版本启动说明.txt \
         docs/答辩演示流程.md docs/deployment.md docs/course-report.md \
         docs/集成系统验收标准.md docs/答辩.pptx "${DOCS[@]:-}"; do
  [[ -n "$d" && -f "$REPO/$d" ]] && cp "$REPO/$d" "$PKG/docs-copy/"
done

# 5) 清单与校验和
cat >"$PKG/MANIFEST.md" <<EOF
# 离线演示包（recon-offline）

- 打包日期：$(date +%Y-%m-%d)
- 仓库快照：git HEAD = $HEAD（repo.tar.gz，git archive 生成）
- 模型：$(ls "$PKG/models" 2>/dev/null | wc -l) 个 GLB
- 前端构建：$([[ -f "$PKG/frontend-dist.tar.gz" ]] && echo "包含（frontend-dist.tar.gz）" || echo "未包含")
- 文档：docs-copy/（启动说明、演示流程、部署、报告、PPT）

## 内容核对

\`\`\`bash
sha256sum -c SHA256SUMS
\`\`\`

## 复现说明

1. 解开 repo.tar.gz；wheelhouse 依赖按 [deployment.md](docs-copy/deployment.md) 离线安装。
2. mock 演示：按 deployment.md「离线演示」节三终端启动。
3. 双卡候选站：按 deployment.md「集成候选站部署」节用 scripts/acceptance_env.sh 启动（需 A800 + 本地盘 scratch）。
4. 断网彩排：按 docs-copy/答辩演示流程.md「离线彩排」节逐项打勾。

禁止内容：真实数据库、生产 .env、demo 凭据、无权分享的原始照片——不出包。
EOF

(cd "$PKG" && find . -type f ! -name SHA256SUMS -print0 \
  | sort -z | xargs -0 sha256sum >SHA256SUMS)

echo "离线包已生成: $PKG"
echo "总大小: $(du -sh "$PKG" | cut -f1)，文件数: $(find "$PKG" -type f | wc -l)"
