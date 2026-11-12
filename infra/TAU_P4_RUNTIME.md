# τ P4 独立售后演示环境

服务绑定本机回环地址。浏览器入口为 [5224](http://127.0.0.1:5224/)，API 为 [18224](http://127.0.0.1:18224/api/readyz)。Compose 项目 `tau-chat-p4` 持有自己的 PostgreSQL、Redis 和 S3 卷。应用核对 PostgreSQL 的 `tau-chat-p4` 集群身份，连接端口分别是 15439、16389 和 18339。检索沿用 `tau-rag` Colima 中的 Milvus 19538，另建业务政策集合。原 τ 政策集合用于独立引用验证，业务政策集合用于当前店铺演示及审批。订单、照片和处置使用已批准的 Aftercare 合成数据。

## 首次启动

在仓库根目录运行以下命令。已启动的 `tau-rag` 可直接使用。启动命令明确指定其 Docker context。

```sh
colima start tau-rag --activate=false --ssh-config=false --disk 8 --root-disk 6 \
  --memory 6 --cpus 3 --vm-type vz --mount none
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-rag.yml up -d --wait
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-p4.yml up -d --wait
```

准备独立的 Python 环境。使用锁定的业务依赖，Milvus、ONNX 和 PDF 辅助依赖单独安装。

```sh
p4_python_env="$HOME/.cache/aftercare-agent/tau-p4-venv"
p4_rag_deps="$HOME/.cache/aftercare-agent/tau-p4-rag-deps"
UV_PROJECT_ENVIRONMENT="$p4_python_env" uv sync --project backend --frozen --no-dev \
  --no-install-project --python 3.11
uv pip install --python "$p4_python_env/bin/python" --target "$p4_rag_deps" \
  onnxruntime==1.23.2 pymilvus==2.6.9 pdfplumber==0.11.9 \
  'pillow>=11,<13' 'python-dateutil==2.9.0.post0'
export PYTHONPATH="$PWD/backend/src:$PWD:$p4_rag_deps"
"$p4_python_env/bin/python" scripts/tau_p4_runtime.py init
"$p4_python_env/bin/python" scripts/tau_p4_runtime.py index
"$p4_python_env/bin/python" scripts/tau_p4_runtime.py verify-reference
```

`init` 迁移并导入合成种子，在独立私有文件 `.runtime/tau-p4-demo/.runtime/users-test.json` 写入客户、客服和审批角色凭据。对已有环境重启时可跳过 `init` 和 `index`。若政策目录文件已经存在，`index` 拒绝覆盖。P3 文档、目录、缓存的默认位置已写在运行脚本中，使用这些本地缓存可以离线完成检索。

## 打开演示

分别在终端启动三个进程。在每个终端设置前一节的 `p4_python_env`、`p4_rag_deps` 和 `PYTHONPATH`。

```sh
"$p4_python_env/bin/python" scripts/tau_p4_runtime.py api --investigation
"$p4_python_env/bin/python" scripts/tau_p4_runtime.py worker --investigation
"$p4_python_env/bin/python" scripts/tau_p4_runtime.py beat
```

API 与 worker 使用同一 V7 调查协议。上面的 API、worker、beat 默认不启用付费模型，beat 只定时处理分析和恢复。前端在 `frontend` 目录启动，使用现有的前端依赖及配置。

```sh
cd frontend
node --input-type=module -e '
  import { createServer } from "vite";
  const server = await createServer({server: {
    host: "127.0.0.1", port: 5224, strictPort: true,
    proxy: {"/api": {target: "http://127.0.0.1:18224"}}
  }});
  await server.listen();
  server.printUrls();
'
```

API 健康后，通过 5224 点击客户、客服、独立审批人或测试员即可进入，不再输入账号密码。四个账号映射到原有用户，原工单和权限不变。当前演示已配置；首次部署可执行：

```sh
"$p4_python_env/bin/python" scripts/setup_demo_login.py \
  --users .runtime/tau-p4-demo/.runtime/users-test.json \
  --output .runtime/tau-p4-demo/login-accounts.json
```

命令交互输入共用演示密码，密码只保存为带独立随机盐的 scrypt 哈希。映射文件权限0600，未提交到版本库。演示角色直达仅在 `demo_role_login_enabled=True` 时开启，P4独立本地运行脚本已启用，默认配置关闭；服务内部仍验证身份和角色。登录接口30次/分钟进程内限流，专用于本机演示，不是生产账户管理系统。当前浏览器验证使用角色按钮，运行 `node scripts/verify_demo_role_login.mjs`。旧密码登录测试仅作为历史记录。
真实模型的受控验证记录见 [P4 验收报告](../reports/tau-p4-live-business-final-20261112.json)。

## 重启和关闭

先在各自的终端停止 beat、worker、API 和前端。保持数据库和对象存储运行时，重新执行「打开演示」各命令可以读回同一会话、记忆和回执。关闭 P4 容器用下列命令。服务停止时保留具名卷。

```sh
env -u DOCKER_HOST DOCKER_CONTEXT=colima-tau-rag docker-compose \
  -f infra/compose.tau-p4.yml stop
```

若同时关闭 P3 检索，按 [P3 运行说明](TAU_RAG_RUNTIME.md) 停止其服务与 profile。恢复时重复「首次启动」中的 Compose 启动命令，再启动应用进程。
