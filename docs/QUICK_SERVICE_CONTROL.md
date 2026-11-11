# 售后项目一键启停

在项目根目录双击即可：

- **启动售后.command**：自动启动 Colima（如未运行），恢复两套已有演示环境。
- **关闭售后.command**：停止所有售后项目容器；虚拟机无人使用时一并关闭。
- **重启售后.command**：重启两套演示，保留数据。
- **查看售后状态.command**：查看运行状态与入口。

脚本不会重建镜像、初始化数据库、删除容器或数据卷，也不会恢复历史付费验收工作器。

## 需要启动什么

| 层级 | 内容 | 用途 |
| --- | --- | --- |
| 虚拟机 | Colima 默认配置，Docker 引擎 | 承载容器；当前为 4 核、8 GB 内存、100 GB 虚拟磁盘，沿用已有设置 |
| 每套演示的基础服务 | PostgreSQL、Redis、对象存储 | 保存业务记录、任务队列、照片 |
| 每套演示的应用服务 | API、Celery Worker、前端 | 客户聊天、客服工作台、后台任务 |
| 演示默认不启动 | Celery Beat 定时调度器 | 保持现有演示结果，不自动恢复历史任务 |

默认两套演示共 **12 个容器**：退款与查询 `http://127.0.0.1:5215`，双商品补证 `http://127.0.0.1:5213`。这两个环境恢复已有记录和配置；不会因为启动脚本而配置真实模型。历史体验环境 `5186` 可单独启动，保留其既有模型配置。

## 命令行

在项目根目录运行：

```sh
./scripts/aftercare.sh start                    # 启动两套演示，必要时启动虚拟机
./scripts/aftercare.sh status                   # 查看两套演示状态
./scripts/aftercare.sh restart                  # 重启两套演示
./scripts/aftercare.sh stop                     # 停止所有售后容器，保留虚拟机
./scripts/aftercare.sh stop --vm                # 停止所有售后容器，再尝试关闭虚拟机
```

只用一套时：

```sh
./scripts/aftercare.sh start --target refund    # 5215：退款和查询
./scripts/aftercare.sh start --target complex   # 5213：双商品补证
./scripts/aftercare.sh restart --target refund
./scripts/aftercare.sh stop --target complex
./scripts/aftercare.sh status --target all      # 包括已停止的历史售后容器
./scripts/aftercare.sh start --target app       # 旧体验环境 5186，含定时任务，可能调用真实模型
```

虚拟机单独控制：

```sh
./scripts/aftercare.sh vm-start
./scripts/aftercare.sh vm-stop
```

`vm-stop` 会先停止售后服务。若有其他项目容器运行（例如本机的 `digital-sentinel`），它会保留虚拟机、列出占用者并返回非零状态。此时售后服务已经关闭；不要为关闭售后而误停其他项目。脚本只操作默认 Colima 的本地 Docker context，不切换你的全局 Docker context。

## 数据与维护

- 使用 Docker `start` / `stop` 复用已有容器，不执行 `down -v`、`rm`、`prune` 或迁移。
- 关闭顺序为定时任务 → 页面/API → Worker → 基础服务；给 Worker 最多 75 秒退出。正在运行真实模型任务时，应先等任务结束再关闭，超时强制停止可能留下待核对调用。
- 启动按依赖顺序进行，检查数据库、队列、页面 `/api/readyz` 和 Worker 响应；再次启动是幂等操作。
- 缺少容器或演示登记文件时会报错，不会自动建一个空环境冒充原环境。
- 两套演示登记文件为 `.runtime/offline-browser-demo-live-success-ordinary-r10.json` 和 `.runtime/offline-browser-demo-live-success-complex-r2.json`。以后更换演示环境时，更新 `scripts/aftercare_control.py` 中的 `DEMO` 映射。
- 此脚本面向当前已有的本机安装。新机器先按 [部署说明](OPERATIONS.md)安装 Colima、Docker CLI 并初始化项目。历史宿主机开发进程不由本脚本管理；当前核对未发现这类售后服务在运行。

场景操作与角色凭据位置见 [演示指南](DEMO_GUIDE.md)。

## 本机验证（2026-11-12）

已实际验证两套演示启动、页面与 Worker 就绪、退款环境单独重启，以及全部售后服务关闭。5 项自动检查覆盖容器归属、关闭顺序、阻止全量历史环境启动和共享虚拟机保护。虚拟机冷启动/整机关闭仅做控制逻辑检查：本机还有其他项目运行，为保留其状态未实际关闭 Colima。
