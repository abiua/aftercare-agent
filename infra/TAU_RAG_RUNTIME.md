# τ-bench P3 本地检索环境

在仓库根目录执行。独立 Colima profile `tau-rag` 使用两个具名存储卷，Milvus 和 PostgreSQL 只在本机回环端口提供服务。本次 ARM64 环境中的两个解包镜像合计约 4.2 GB，另需为虚拟机和模型留出空间。

```sh
colima start tau-rag --activate=false --ssh-config=false --disk 8 --root-disk 6 \
  --memory 6 --cpus 3 --vm-type vz --mount none
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-rag.yml up -d --wait

uv venv --python 3.12.12 .runtime/tau-rag-p3/venv
uv pip install --python .runtime/tau-rag-p3/venv/bin/python \
  -r scripts/tau_rag_requirements.txt
```

首次加载下载两个固定 revision 的 CPU ONNX 模型。已缓存时可设置 `HF_HUB_OFFLINE=1`。以下例子使用 P2 的零售政策导出目录和源 Markdown，输出路径须尚不存在。

```sh
tau_python=.runtime/tau-rag-p3/venv/bin/python
tau_model_cache=.runtime/tau-rag-p3/models
tau_document=.runtime/tau-p2-root-20261112/policy
tau_revision=da0835f6c67baef58bdda18f2a3b037dfb3a1754

"$tau_python" scripts/verify_tau_rag_storage.py --model-cache "$tau_model_cache" \
  --output reports/tau-p3-storage-local.json
"$tau_python" scripts/evaluate_tau_rag.py "$tau_document/original/policy.md" \
  --model-cache "$tau_model_cache" --database-stores \
  --output reports/tau-p3-database-local.json
```

生命周期脚本实际停止并重新启动这两个容器。报告记录重启前后容器 ID、启动时间、健康状态、数据库版本与存储结果。评测为两个后端使用同一批查询和文档向量，采用精确余弦检索。

持久目录由一次明确的建库操作创建。后续查询读取同一目录绑定的库表或集合。两个命令中的 `--store` 可同时替换为 `pgvector`，并使用独立的目录文件。

```sh
tau_catalog=.runtime/tau-rag-p3/milvus-catalog-local.json

"$tau_python" scripts/tau_rag.py "$tau_document" --build-index --store milvus \
  --catalog "$tau_catalog" --policy-key tau_retail --role policy_reader \
  --version "$tau_revision" --model-cache "$tau_model_cache"
"$tau_python" scripts/tau_rag.py "$tau_document" 'Can I return a delivered order?' \
  --mode rerank --store milvus --catalog "$tau_catalog" --policy-key tau_retail \
  --role policy_reader --version "$tau_revision" --model-cache "$tau_model_cache"
```

停止后可用上述 profile 启动和 Compose 命令重新打开同一持久目录。

```sh
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-rag.yml stop
colima stop tau-rag
```
