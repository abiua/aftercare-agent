#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
EVAL_PYTHON="${AFTERCARE_EVAL_PYTHON:-$HOME/.cache/aftercare-agent/tau-p4-venv/bin/python}"
OFFICIAL_PYTHON="${AFTERCARE_TAU_OFFICIAL_PYTHON:-$ROOT/.runtime/tau-official-venv/bin/python}"
LEGACY_PYTHONPATH="$ROOT/backend/src:$ROOT:$ROOT/scripts:$HOME/.cache/aftercare-agent/tau-p4-rag-deps${PYTHONPATH:+:$PYTHONPATH}"
case "${1:-help}" in
  official)
    shift
    exec env -u PYTHONPATH "$OFFICIAL_PYTHON" scripts/run_tau_official_campaign.py "$@"
    ;;
  models) exec env PYTHONPATH="$LEGACY_PYTHONPATH" "$EVAL_PYTHON" scripts/tau_p9_runtime.py models ;;
  api|worker) ACTION="$1"; shift; exec env PYTHONPATH="$LEGACY_PYTHONPATH" "$EVAL_PYTHON" scripts/tau_p9_runtime.py "$ACTION" "$@" ;;
  *) cat <<'HELP'
用法：scripts/aftercare-eval.sh <动作> [选项]
  official run --scope smoke|full --directory 路径 --max-usd 金额 --max-calls 次数 [--live]
    运行固定版本官方 τ²-bench Retail test，同模型比较三种 Agent 架构。
    省略 --live 时仅输出冻结清单。付费运行须使用新目录。
  official repair 原批次路径 --max-usd 金额 [--live]
    在独立目录重试原批次未评分的技术异常，最多一次，总额不超过 $3。
  official summarize 路径 [--publish] [--mark-interrupted | --include-repair]
    按冻结清单汇总官方评分及运行异常；--publish 更新评测看板。
  models                 生成带视觉能力的任务路由配置
  api [--live]           启动18225售后API；默认离线，--live开启有预算的模型调用
  worker [--live]        使用同一配置启动售后任务Worker
HELP
    ;;
esac
