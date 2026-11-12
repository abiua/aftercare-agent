# 官方 τ²-bench Retail 复现环境

`scripts/setup_tau_official.sh` 使用 Git、uv 和 Python 3.12。它从 `data/tau/manifest.json` 读取固定提交，在 `.runtime/tau-official-source` 检出该提交，并校验清单中全部源文件的 SHA-256。已有检出若存在本地改动或提交不符，脚本会退出并保留现场。

```bash
bash scripts/setup_tau_official.sh
bash scripts/setup_tau_official.sh --check
```

首次安装在 `.runtime/tau-official-venv` 创建独立环境，以官方 `uv.lock` 安装运行依赖。已有环境只检查 Python 和关键依赖版本。运行器从固定检出的 `src` 目录加载官方代码，因此环境可以仅安装依赖。当前环境最初使用以下命令安装，安装脚本为新环境额外指定 `--no-install-project`。

```bash
UV_PROJECT_ENVIRONMENT=../tau-official-venv \
  uv sync --project .runtime/tau-official-source --locked --no-dev --python 3.12
```

从项目根目录运行适配器离线专项测试。当前环境实测两项通过，覆盖并行角色隔离、预算上下文传递、官方只读查询和查询范围限制。

```bash
LITELLM_LOCAL_MODEL_COST_MAP=True PYTHONPATH=.runtime/tau-official-source/src \
  .runtime/tau-official-venv/bin/python -m unittest discover \
  -s scripts/tests -p test_tau_official_agents.py
```

已有结果可以重新汇总。

```bash
.runtime/tau-official-venv/bin/python scripts/run_tau_official_campaign.py \
  summarize .runtime/tau-official-full
```

运行参数可通过 `scripts/run_tau_official_campaign.py run --help` 查看。真实模型调用需要显式传入 `--live`、调用上限和费用上限。
