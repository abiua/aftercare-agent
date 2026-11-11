# 本地部署与演示

基线 Compose 使用模拟业务数据、离线 Agent、私有材料对象和模拟款项执行。本机当前体验环境已显式启用 live-hybrid，仍使用模拟订单与款项。体验与测试有独立容器网络、数据库、队列和对象卷。测试中台的写入与运行操作使用测试环境。

| 环境 | 工作台 | API | PostgreSQL | Redis | 对象入口 |
| --- | --- | --- | --- | --- | --- |
| 体验 `app` | 5186 | 8020 | 55450 | 56379 | 58350 |
| 测试 `test` | 5187 | 8022 | 55451 | 56380 | 58351 |

这些端口仅绑定到本机。前端端口和 API 端口由对应环境独占。若本机已经启动 Vite、Uvicorn 或完整容器栈，先确认该端口上的运行实例，再选择其中一个入口。

## 全新安装离线基线

以下命令适用于新安装或对应端口空闲的环境。维护本机已经运行的体验栈，请用下文的维护入口。使用 Docker Engine 与 Compose，在项目根目录执行。使用能访问 Docker 的非 root 账户，先生成仅本机可读的运行配置，再将该账户的 UID 和 GID 传给容器。

```sh
python3 scripts/setup_runtime.py
export AFTERCARE_UID="$(id -u)"
export AFTERCARE_GID="$(id -g)"
docker compose -f infra/compose.yml -f infra/compose.application.yml --profile test config --quiet
scripts/build_images.sh
docker compose -f infra/compose.yml -f infra/compose.application.yml --profile test up --no-build -d
```

构建脚本分别暂存后端与前端需要的文件，默认生成 Compose 使用的本地镜像。只重建一个镜像时，用 `scripts/build_images.sh backend` 或 `scripts/build_images.sh frontend`。

使用独立 Compose 命令时，将 `docker compose` 写成 `docker-compose`。体验环境将 `test` 换成 `app`。第一次构建会下载冻结依赖。Linux 使用官方 PyTorch 2.15.0 CPU 包，macOS 保持同版本的 PyPI 包。镜像构建上下文和镜像中排除运行配置与模型密钥。前端镜像只提供静态工作台与同源 API 转发。[PyTorch CPU 包索引](https://download.pytorch.org/whl/cpu/torch/)

初始化任务等 PostgreSQL 就绪并连接对象存储后执行数据库迁移，导入模拟业务事实，初始化图检查点并生成身份凭据。随后 API、任务进程和定期恢复进程启动。已有运行配置与业务历史由初始化流程保留。

```sh
docker compose -f infra/compose.yml -f infra/compose.application.yml --profile test ps -a
curl -fsS http://127.0.0.1:8022/api/healthz
curl -fsS http://127.0.0.1:5187/api/readyz
```

`healthz` 检查 API 是否可访问，`readyz` 额外检查数据库和迁移。通过页面端口请求 `readyz` 同时检查 Nginx 转发。Redis 和任务进程有各自健康检查。若初始化退出失败，先查看本环境初始化日志和服务状态。

```sh
docker compose -f infra/compose.yml -f infra/compose.application.yml --profile test logs --tail 100 init-test api-test worker-test beat-test
```

停用测试环境时用同一命令前缀运行 `stop`。保留项目卷和 `.runtime`，下次启动会复用。数据库与材料对象的备份与隔离恢复流程见 [备份与恢复](BACKUP_RESTORE.md)。

## 体验容器的显式模型与混合检索模式

基线体验 Compose 采用离线规则和词法检索。以下配置用于从基线首次启用真实模型和本地混合检索。维护本机现有的 live-hybrid 栈，请用下一节保留镜像覆盖。`infra/compose.live.yml` 只为 `api-app` 和 `worker-app` 增加专用的只读挂载。容器使用已核验的注册文件和模型凭据，嵌入从本机快照读取。

准备已有的 `.runtime/models.json` 与 `.runtime/model-secret.json`，两个文件使用 0600 或 0400 权限。注册项的 `api_key_env` 填写 `AFTERCARE_OPENAI_KEY`。凭据文件使用已有的 `api_key` 字段，容器启动时读取文件，Compose 配置只引用文件路径。向量快照需要完整的模型权重、分词和 pooling 文件，并使用实际的 40 位小写版本号。使用可从容器读取权重的本机快照目录。本机首次从 Documents 的 FileProvider 目录挂载时读取失败，错误为 Errno 35。当前使用本机缓存目录，34 个文件的 SHA-256 与来源一致。[缓存核验](../reports/deployment/container-bge-cache.json)

```sh
export AFTERCARE_UID="$(id -u)"
export AFTERCARE_GID="$(id -g)"
export AFTERCARE_EMBEDDING_REVISION=7999e1d3359715c523056ef9478215996d62a620
export AFTERCARE_EMBEDDING_DIR="/Users/mbw/.cache/aftercare-agent/models/$AFTERCARE_EMBEDDING_REVISION"
docker compose -f infra/compose.yml -f infra/compose.application.yml -f infra/compose.live.yml --profile app config --quiet
scripts/build_images.sh
docker compose -f infra/compose.yml -f infra/compose.application.yml -f infra/compose.live.yml --profile app up --no-build -d
curl -fsS http://127.0.0.1:5186/api/readyz
```

首次启用该版本时，从体验容器建立政策向量索引。此步骤读取本机嵌入模型并写入体验库的政策向量表。本机首次成功建立 14 条索引，提供方调用数为 0。[索引记录](../reports/deployment/app-live-index.json)

```sh
docker compose -f infra/compose.yml -f infra/compose.application.yml -f infra/compose.live.yml --profile app exec -T api-app python - <<'PY'
from aftercare.runtime import open_runtime

with open_runtime("app") as (_, _, _, agent):
    print(agent.index_policies())
PY
```

真实模型运行按注册文件中的可用性和预算执行。体验 API 和 Worker 共享 app 数据库中的限流状态，test 数据库独立。切回默认离线模式时，使用基线两个 Compose 文件重建体验服务配置。隔离测试容器仅使用默认配置，宿主机测试模式由各自的本地配置决定。

报告导入的原始 UTF-8 JSON 上限为 8 MiB。导入路由的代理和后端允许最多 17 MiB 的外层请求，以容纳 JSON 转义。私有材料上传继续使用 8 MiB 文件上限和原有的外层额度。容器内部 S3 地址用于读写，材料 `/link` 使用宿主可达地址直接签名。默认使用本机配置中容器改写前的 S3 地址，可用 `object_presign_endpoint` 显式覆盖。app 签名原始 URL 可直接访问，未改 Host，配置 60 秒后约 65 秒实测返回 403。浏览器页面上传交互仍待单独验收。[app 签名验收](../reports/deployment/container-material-link.json) · [test 过期验收](../reports/material-expiry-http.json)

## 维护本机现有体验栈

当前体验前端使用 `aftercare-frontend:acceptance-20261110-financial`，`init-app`、`api-app`、`worker-app` 和 `beat-app` 的镜像覆盖仍是 `aftercare-backend:acceptance-20261110-context`。从项目根目录加载上节的 UID、GID 和 BGE 环境变量，并保持下面四个 Compose 文件的顺序。只读状态检查如下。[前端部署记录](../reports/deployment/financial-ui-app.json) · [后端部署记录](../reports/deployment/recovery-context-app.json)

```sh
docker compose -f infra/compose.yml -f infra/compose.application.yml \
  -f infra/compose.live.yml -f .runtime/app-acceptance-images.yml --profile app ps -a
curl -fsS http://127.0.0.1:5186/api/readyz
```

只更新体验前端时，先运行 `AFTERCARE_IMAGE_TAG=next-frontend-tag scripts/build_images.sh frontend`，将 `next-frontend-tag` 换成本次的新标签。把覆盖文件的 `frontend-app.image` 改为 `aftercare-frontend:<新标签>`，再用下面的服务定向命令复用已经运行的 API，避免重新运行初始化任务。命令结束后重复上面的健康检查，并核对首页引用的新静态资源。

```sh
docker compose -f infra/compose.yml -f infra/compose.application.yml \
  -f infra/compose.live.yml -f .runtime/app-acceptance-images.yml --profile app \
  up -d --no-build --no-deps --force-recreate frontend-app
```

本机测试栈使用容器基础设施、宿主机 API `8022`、宿主机任务进程和静态预览 `5187`。访问现有入口可直接使用 `.runtime/users-test.json`。已有服务监听时，请复用当前实例，避免再启动完整 `test` 应用容器、第二套 API/Worker/Beat 或另一套前端。下面的开发命令用于对应环境空闲时启动。

2026-11-10 场景指标验收时，测试 API 的 PID 为 `64406`，Worker 为 `64441`，Beat 为 `64453`。`5187` 首页引用 `/assets/index-BGOLR24m.js`。PID 为当次记录，维护时以实际进程为准。同期体验栈仍使用上述 `financial` 前端与 `context` 后端。[测试进程记录](../reports/deployment/scenario-test-services.json) · [测试静态资源](../reports/deployment/scenario-wording-test-ui.json)

## 本地开发与检查

只需要运行基础设施时，使用 `infra/compose.yml`。准备运行配置后，可从宿主机启动测试 API、任务进程和前端。以下各命令按顺序在对应目录执行，长运行命令各用一个终端。

```sh
python3 scripts/setup_runtime.py
docker compose -f infra/compose.yml --profile test up -d

cd backend
uv sync --frozen --python 3.11
AFTERCARE_PROFILE=test uv run --frozen alembic upgrade head
uv run --frozen python -m aftercare.manage bootstrap --profile test
AFTERCARE_PROFILE=test uv run --frozen uvicorn aftercare.api:create_app --factory --host 127.0.0.1 --port 8022
```

任务进程和定期恢复进程在 `backend/` 下分别执行。

```sh
AFTERCARE_PROFILE=test uv run --frozen celery -A aftercare.worker worker --loglevel=INFO --concurrency=1
AFTERCARE_PROFILE=test uv run --frozen celery -A aftercare.worker beat --loglevel=INFO --schedule=/tmp/aftercare-test-beat
```

在 `frontend/` 下用 Node 24 安装并启动页面。

```sh
npm ci --no-audit --no-fund
npm run dev:test
```

在根目录运行 `scripts/check.sh pure`，复验冻结数据、离线后端、前端代码、事件协议与生产构建。GitHub Actions 使用同一入口。真实 PostgreSQL 检查使用单独入口 `AFTERCARE_RUN_PG=1 scripts/check.sh pg`，依赖已就绪的隔离测试数据库、对象存储、Redis 和任务进程。它会连接并写入测试环境，执行前协调同一环境的其他检查。

### 独立浏览器回归

在项目根目录运行 `frontend/tests/browser/run-local.sh`。脚本使用 Node 24 和 Python 3，将构建文件和锁定依赖暂存到本机缓存目录，结束时删除本轮暂存。默认复用 Playwright 对应版本的 Chromium Headless Shell。首次缺少浏览器时，运行 `AFTERCARE_INSTALL_FIXTURE_BROWSER=1 frontend/tests/browser/run-local.sh` 安装。默认页面端口为 5196，冲突时可通过 `AFTERCARE_FIXTURE_PORT` 指定其它空闲端口。

GitHub Actions 的 `browser-fixtures` 单独运行受控浏览器用例。本机 2026-11-10 场景指标首版通过 18 项，覆盖登录与角色入口、售后确认、上传表单、财务重复提交、人工复核、版本对照和场景下钻。“未确定”文案修订后单独复跑 1 项相关用例，通过分母及文案断言。受控浏览器使用生产静态页面及替身 API。上传用例验证浏览器发送的照片及表单字段。CI 失败时保留截图和追踪文件供复现。[首版浏览器回归](../reports/frontend-scenario-controlled-browser.log) · [文案专项](../reports/frontend-scenario-wording-browser.log)

### 使用外置 Python 依赖环境

当 macOS 的项目目录按需加载文件时，可以把运行依赖和 Python 字节码缓存放在本机缓存目录。源码和运行配置继续从项目目录读取。先按上节准备测试基础设施、迁移与登录凭据。

首次准备环境时，在项目根目录执行。

```sh
cd backend
aftercare_runtime_cache="$HOME/.cache/aftercare-agent"
mkdir -p "$aftercare_runtime_cache/pycache"
uv lock --offline --check
UV_PROJECT_ENVIRONMENT="$aftercare_runtime_cache/backend-venv" \
  UV_PYTHON_DOWNLOADS=never \
  uv sync --offline --frozen --no-dev --python 3.11 --link-mode clone
```

已有外置环境时，从项目根目录在每个启动终端载入以下设置。

```sh
cd backend
aftercare_runtime_cache="$HOME/.cache/aftercare-agent"
aftercare_python="$aftercare_runtime_cache/backend-venv/bin/python"
export AFTERCARE_PROFILE=test
export PYTHONPYCACHEPREFIX="$aftercare_runtime_cache/pycache"
```

测试 API、任务进程和定期恢复进程分别使用各自终端中的一条命令。定期恢复进程每个环境运行一份。

```sh
"$aftercare_python" -m uvicorn aftercare.api:create_app --factory --host 127.0.0.1 --port 8022
"$aftercare_python" -m celery -A aftercare.worker worker --loglevel=INFO --concurrency=1
"$aftercare_python" -m celery -A aftercare.worker beat --loglevel=INFO --schedule="$aftercare_runtime_cache/beat-test"
```

`AFTERCARE_PROFILE=test` 使用独立测试环境。模型策略继续由本项目的运行配置决定。API 就绪后可读取 `http://127.0.0.1:8022/api/readyz`。本机已用冻结锁离线准备 108 个运行依赖，并在阻止网络连接的三次新进程中验证 FastAPI、Celery、Uvicorn 与应用模块导入。依赖环境约 1.0 GiB，导入预热后的字节码缓存约 36 MiB。

## 操作演示

初始化后的访问凭据保存在对应的 `.runtime/users-app.json` 或 `.runtime/users-test.json`。从本机文件复制当前角色的凭据到登录表单，避免将凭据放进截图、命令日志或提交记录。

离线演示使用新建的离线基线，或复用本机测试工作台 `5187` 并在评测中选择 `fixture`。本机体验工作台 `5186` 启用了真实模型，提交助手运行或继续请求时可能产生调用费用。测试中台的运行、复核和导入使用 `5187`。

1. 客户打开「描述问题发起售后」，填写破损情况，按页面提示核对本人已签收订单和售后类型，选择「确认并建单」。客服打开工单的「助手协作」，选择三 Agent 运行，观察缺照片时暂停。
2. 客户提交商品照和外包裹照。客服继续原运行，看到等待人工审核后查阅私有照片、记录审核结论，再继续同一运行。刷新页面，核对工单与运行记录。
3. 客服核对订单和可用额度，提交合法处理方案。审批人登录审批，客服回到「方案与回执」发起模拟执行并检查回执。如果回执未知，在原记录使用「核对执行结果」。
4. 客户另开退款进度工单，确认页面展示账本中的已登记状态。错发工单按页面要求提交实物和标签照片，可在库存允许时提换货方案。
5. 测试人员进入测试工作台，选择数据集与少量案例，启动受控实验。核对已启动、已结束、未运行和评分分母，查看实际与预期，保存带依据的人工复核，再导出 JSON 或 CSV。
6. 对同一批案例启动一个对照实验。分别固定模型策略比较 Agent 数量，或固定 Agent 数量比较模型策略。进入「版本对照」查看新增失败和修复。

首页的“已办结工单”按列表返回的业务状态 `RESOLVED` 计数。材料页根据详情的当前补证要求提示所需照片。原“待补充材料”卡片只读到了列表的业务状态，新建工单可能仍为 `OPEN`，助手暂停也可能是在等待人工审核，因此这个口径无法准确汇总当前客户待补证的工单。

## 本批验证边界

- Compose 两个环境通过配置解析，并核对了各自网络和端口。隔离测试入口在未显式启用时实际拒绝运行。
- 本地离线检查覆盖冻结数据哈希与字节重建、前端协议、代码检查和生产构建。2026-11-10 场景指标轮次的完整后端离线测试记录 522 项通过、1 项跳过、11 项未选中。前端文案修订版通过 31 项纯测试、Lint、TypeScript 和 Vite 构建。[后端纯测试日志](../reports/backend-scenario-root-tests.log) · [前端构建日志](../reports/frontend-scenario-wording-checks.log)
- 场景指标独立测试环境的真实 HTTP 验收核对 126 项，覆盖 3 个场景、6 次运行和 52 项指标。浏览器在同一实验中只读核对分母、打开第二次运行并刷新深链接，场景选择由用户协助一次。该实验使用合成业务，模型调用和人工复核均为 0。接口 409 使用刻意不匹配的摘要验证，实际浏览器未触发并发复核冲突。[真实接口](../reports/scenario-metrics-http.json) · [真实浏览器](../reports/browser-scenario-metrics.json)
- 前端镜像在只含源码的临时构建目录中构建成功。使用无网络容器通过 `nginx -t`，确认最终进程身份为 UID 101，并读回首页引用和站点图标。整仓库的传统构建在收集上下文时长时间未返回，随后终止并切换到临时构建目录。
- 后端镜像在 Linux arm64 的干净构建目录中完成冻结安装，镜像大小约 401 MB。断网容器确认默认 UID 10001、项目路径、两个环境的配置转换、无关数据库地址拒绝、API 工厂与存活路由、任务入口和三个迁移脚本的静态导入。镜像中的 PyTorch 2.15.0+cpu 通过张量计算。另在 macOS arm64 复验冻结安装与导入，并通过 333 项后端离线测试。日志见 `reports/deployment/`。
- 首轮断网检查发现 Celery 重复任务名警告。修复后，在同一断网进程中分别构造体验和测试环境的 API 发布器及任务入口，验证通过。最新镜像重用了后端依赖与前端依赖层。前端最终版通过容器内代码检查、类型检查、3 项事件测试和 Nginx 配置检查。
- 体验 Compose 栈已完成启动。通过 Nginx 真实请求创建工单，容器任务进程从队列读取并将离线运行推进至等待补证。检查记录见 [体验栈流程](../reports/compose-app-flow.json)。
- 最终后端镜像通过源码哈希和运行指纹对照。断网模拟代码变化，确认启动保护在打开运行资源前返回 503。检查记录见 [最终后端镜像](../reports/deployment/final-backend-verify.log)。
- 体验 live-hybrid 叠加层通过 Compose 配置与真实容器在线验收。断网 Nginx 中，9 MiB 导入请求转发至无监听的合成上游后返回 502，9 MiB 材料请求与 18 MiB 导入请求均由代理返回 413。
- 隔离测试 Compose 栈的完整冷启动与 GitHub 托管工作流待单独验收。宿主机真实 API、浏览器及备份恢复证据参见根集成报告。

### 2026-11-10 最新本地体验部署

入口为 <http://127.0.0.1:5186>，当前使用 live-hybrid。后端为 `aftercare-backend:acceptance-20261110-context`，前端为 `aftercare-frontend:acceptance-20261110-financial`。前端更新后，经同源入口验证了就绪状态和静态资源，后端容器保持原身份。下列迁移和在线记录是此前对应轮次的证据。[当前前端部署](../reports/deployment/financial-ui-app.json) · [当前后端部署](../reports/deployment/recovery-context-app.json)

本轮已单独迁移至 `20261110_intake_eval`，再冷启动 API、Worker、Beat、前端。未重新初始化已有数据库。停写期间对原30张表的原列进行行数和内容摘要对照，登录凭据文件摘要、权限和修改时间均保持一致。随后通过同源 Nginx 入口完成客户澄清、确认、幂等建单、启动和缺证暂停，持久队列领取一次完成，0模型调用。

以上为原离线升级路径。[部署与静态资源](../reports/deployment/app-acceptance-deployment.json) · [升级数据保留](../reports/deployment/app-upgrade-preservation.json) · [离线入口闭环](../reports/deployment/app-acceptance-intake.json)

随后启用 live-hybrid。容器 API 用 Terra 预判退款进度 1 次，容器 Worker 用 Sol 2 次，共 3 次调用，按返回用量和注册表价格估算 0.0060415 美元。Worker 一次领取后完成 STATUS_ONLY，使用 HYBRID 检索，案情、订单、账本、方案和审批快照保持一致。[在线验收](../reports/deployment/app-live.json)

空卷初始化另有独立验收。客服已在真实页面预览材料并提交审核结论，该次使用事先经 HTTP 上传的合成材料。客户页面上传仍待单独验收。[空卷报告](../reports/deployment/fresh-bootstrap-acceptance.json) · [材料预览与审核](../reports/browser-material-review.json)

体验登录使用本机 `.runtime/users-app.json` 对应角色的 `token`。凭据文件仅保存在本机。

### 2026-11-10 已升级体验版：场景统计与逐项复核

体验UI5186及API/Worker/Beat已升级acceptance-20261110-scenario，含质保资料未知转人工、逐项人工复核和场景统计；旧context/financial镜像与覆盖文件保留供回退。升级前无活跃任务，停止应用写入进程后对36张非迁移public表全列计数/内容摘要、登录凭据摘要/权限/mtime及迁移版本进行前后核对，全部一致。存储PG/Redis/S3容器身份与启动时间不变。API/Worker/前端healthy，Beat running；同源readyz200、匿名401、客服身份200、历史工单可读。JS/CSS与容器及测试5187产物逐字节一致。无新模型调用，不是新质量批准。证据reports/deployment/scenario-app.json、scenario-app-preservation.json、scenario-app-build.json；实际浏览器场景链路证据仍在测试5187（含一次人工选择协助）。

### 2026-11-10 评测进度测试栈升级

测试库迁移到 `20261110_eval_progress`，原有 4,525 张表的数据按旧列口径核验一致。测试 API/Worker/Beat 已重启；端口仍为 8022/5187，页面资源为 `index-CI_pd3xX.js`。当次 PID 和进程句柄见[进程记录](../reports/deployment/progress-test-services.json)，使用前须核对实际进程。三条 fixture 经真实 API/Celery 完成，12 条进度事件补读通过且零模型调用。体验版 5186 保持上一部署。本条不代表真实浏览器工作台或正式质量验收通过。

### 2026-11-10 进度版体验部署

5186/8020 已统一升级 `acceptance-20261110-progress`。迁移为 `20261110_eval_progress`，36 张原业务表按原列投影、凭据摘要/权限/修改时间保持一致。API/Worker/页面健康，Beat 运行，存储容器 ID 未变，历史工单可读，页面字节与已验测试构建一致。旧镜像配置在 `.runtime/app-before-progress-images.yml`，升级前备份已在独立测试数据库和对象桶恢复验证。回退旧镜像时新增可空字段及事件表可保留，不应直接执行破坏性降级。此次仅部署与只读验收，0 新模型调用。[部署报告](../reports/deployment/progress-app.json) · [数据保留](../reports/deployment/progress-app-preservation.json) · [备份恢复](../reports/deployment/progress-app-backup-restored.json)

### 2026-11-10 指定退款查询部署

5186/8020 已升级 `acceptance-20261110-refund-fixed`，迁移仍为 `20261110_eval_progress`。37 张旧表和凭据均保持一致，存储容器未重建；健康、历史工单、查询能力和未命中 404 已回读。页面为 `index-DMnaLJjg.js`，与已验测试构建一致。旧镜像配置保留在 `.runtime/app-before-refund-images.yml`。测试接口 8022 与页面 5187 同步更新，当次进程见 [记录](../reports/deployment/refund-test-services.json)。[部署验收](../reports/deployment/refund-app.json) · [浏览器只读验收](../reports/browser-refund-lookup.json)。正式评测当前只读快照来源为 NATIVE_LEDGER_ONLY，仍不能正式冻结或发布。

### 当前镜像的独立空卷全栈验证

`python scripts/verify_fresh_bootstrap.py --run-isolated-docker --output <新的报告路径>` 使用本地已存在的 formal 后端与 refund-fixed 页面镜像，不拉取镜像。每次创建带唯一所有权标签的内部网络、PG/S3 空卷与离线配置；检查初始化幂等、API/Nginx 鉴权、Worker/Beat 及缺照任务，然后只清理本次资源。无宿主端口、真实模型或现有工单写入。当前实测通过，见 [报告](../reports/deployment/fresh-formal-stack.json)。
