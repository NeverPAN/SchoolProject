#!/usr/bin/env bash
# 环境预检（指南 §9.2）
echo "== GPU ==";  nvidia-smi 2>/dev/null | head -12 || echo "no nvidia-smi"
echo "== CPU ==";  nproc; lscpu 2>/dev/null | grep "Model name"
echo "== MEM ==";  free -h | head -2
echo "== DISK =="; df -h / /dev/shm 2>/dev/null
echo "== Python =="; python3 --version
echo "== Node ==";  node --version 2>/dev/null || echo "no node"
echo "== Redis =="; redis-cli ping 2>/dev/null || echo "redis not running"
echo "== PostgreSQL =="; psql --version 2>/dev/null || echo "no psql"
echo "== COLMAP =="; colmap --version 2>/dev/null || echo "no colmap"
