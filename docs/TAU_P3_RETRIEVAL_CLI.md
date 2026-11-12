# P3 离线检索命令行

命令从 P2 导出目录读取原文和结构，重新校验来源。例子使用固定零售政策和 `policy_reader` 离线角色。政策来源没有生效日期，按固定版本检索时返回 `date_applicability: unknown`。

```bash
version=$(python3 -c 'import json; print(json.load(open("data/tau/manifest.json"))["revision"])')
python3 scripts/tau_rag.py .runtime/tau-p2-root-20261112/policy \
  'Can I return a delivered order?' --policy-key tau_retail --role policy_reader \
  --version "$version" --mode bm25
```

数据库检索使用 `scripts/tau_rag_requirements.txt` 中的依赖。启动 P3 独立数据库后，用同一 Python 环境分两次运行。将 `pgvector` 换成 `milvus` 时，请同时换用一个新的 catalog 文件名。

```bash
python3 scripts/tau_rag.py .runtime/tau-p2-root-20261112/policy \
  --policy-key tau_retail --role policy_reader --version "$version" \
  --model-cache .runtime/tau-rag-p3/models --store pgvector \
  --catalog .runtime/tau-rag-p3/retail-pgvector.json --build-index

python3 scripts/tau_rag.py .runtime/tau-p2-root-20261112/policy \
  'Can I return a delivered order?' --policy-key tau_retail --role policy_reader \
  --version "$version" --model-cache .runtime/tau-rag-p3/models --store pgvector \
  --catalog .runtime/tau-rag-p3/retail-pgvector.json --mode rerank
```

`--build-index` 生成并回读单独的数据库表或集合，随后发布 catalog。检索打开该索引，核对本地来源、切片、嵌入模型及数据库条目，再编码查询。修改来源、角色、切片参数或嵌入模型后，应使用新 catalog 重新构建。Catalog 是离线快照，数据库更新、删除和重建的验收独立运行。可通过 `TAU_RAG_PG_DSN` 或 `TAU_RAG_MILVUS_URI` 指向 P3 专用数据库。

固定切片使用 `--strategy fixed`，窗口和重叠可分别用 `--chunk-size` 和 `--overlap` 设置，默认 450/70 字符。模型在输入超出 token 上限时拒绝编码，具体坏例及本次语料长度见 [P3 检索坏例](TAU_P3_BAD_CASES.md)。
