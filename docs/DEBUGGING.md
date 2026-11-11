# 从浏览器进入建单断点

Notion 的 U1 验收要求页面操作能停到用户业务函数。本入口在本机单独打开正式 React 工作台，将真实的工单请求交给 FastAPI 路由、原鉴权和 `BusinessService.create_case`。标准库 `pdb` 在业务方法入口暂停。运行环境使用本轮临时 SQLite、冻结的模拟订单和随机生成的调试凭据。此入口的覆盖范围是客户登录、本人订单、结构化建单及查询。

| 进程 | 地址 |
| --- | --- |
| 调试 API | `http://127.0.0.1:18092` |
| 调试页面 | `http://127.0.0.1:15192` |

两个进程只绑定回环地址，端口冲突时先退出并确认占用者。SQLite 和 `users-debug.json` 置于本轮专用临时目录。凭据文件权限为 0600，终端只显示路径。调试入口与现有 app/test 的 PostgreSQL、S3、队列、模型和登录文件分开。

## 启动

从项目根目录在交互式终端 A 运行。使用已经准备好的锁定 Python 环境，其他安装路径见[本地开发](OPERATIONS.md#本地开发与检查)。

```sh
debug_python="$HOME/.cache/aftercare-agent/backend-venv/bin/python"
PYTHONPATH="$PWD/backend/src" "$debug_python" scripts/debug_api.py
```

从项目根目录另开终端 B，进入下面的临时子 Shell。若前端依赖尚未准备，先按[开发说明](OPERATIONS.md#本地开发与检查)从锁安装。调试页面使用正式前端的生产构建，输出到独立临时目录。

```sh
(
  set -eu
  cd frontend
  debug_dist="$(mktemp -d)"
  trap 'rmdir "$debug_dist" 2>/dev/null || rm -rf -- "$debug_dist"' EXIT
  npm run typecheck
  ./node_modules/.bin/vite build --outDir "$debug_dist"
  ./node_modules/.bin/vite preview --config ../scripts/debug_vite.config.mjs --outDir "$debug_dist"
)
```

## 验收动作

1. 打开 <http://127.0.0.1:15192>。从终端 A 显示的私有文件复制 `C001` 的 `token` 到登录框。打开「新建售后工单」，选择本人订单和「商品破损」，填写「隔离调试模拟破损建单」，点击「提交工单」。
2. 浏览器应保持提交中。终端 A 应出现 `(Pdb)`，停在 `backend/src/aftercare/business/service.py` 的 `create_case()`。输入 `where` 查看从业务路由到业务方法的调用栈。输入 `p user.role` 应显示 `'customer'`，`p intent` 应显示 `'damaged'`，`p expected_version` 显示提交的订单版本。
3. 输入 `continue`。浏览器应打开新工单详情。刷新并核对同一工单编号及描述。用独立浏览器窗口登录 `C002` 并访问同一个详情地址，应拒绝读取。只记录函数、行号、角色、工单标识及回读结果，截图时收起凭据和请求头。
4. 如需中止，优先在函数入口输入 `quit`，页面应提示请求已中止。在函数中途停止过时先回读，避免重提。结束后先关闭调试标签页，再在两个终端分别按 Ctrl+C。正常退出会清理各自本轮临时目录。异常退出遗留目录以本轮输出或分配路径为准。

已完成进程内检查及下述真实浏览器到终端断点的串行实操；浏览器选择订单和售后类型使用过一次人工协助。此处只验证建单断点。PG 锁、异步任务、私有材料、模型调用和重启恢复分别参见[集成验收](HANDOFF_INTEGRATION.md)。

### 2026-11-10 实际页面断点补验

浏览器经一次人工选择订单/售后类型协助，工具填写描述并提交；真实POST进入BusinessService.create_case第367行，在终端读取customer/damaged/O0079/version1，continue后201并显示新工单。独立HTTP本人200、其他客户404。浏览器仅1次POST；调试入口未提供可选intake能力接口，该GET404及回退结构化建单已记录。页面/终端/临时SQLite链路已实测，非共享PG或队列/模型调试。会话与两进程已结束，专属临时数据已清理。证据reports/business-breakpoint-browser.json、business-breakpoint-terminal.txt、business-breakpoint-readback.json。此前本项NOT_RUN由此更新；完整3–5分钟演示计时仍未运行。

## 使用 VS Code

在 macOS 或 Linux 用 VS Code 打开项目根目录。启用 Python 与 Python Debugger 扩展。运行「Python: Select Interpreter」，选择已按锁准备的 Python。可选项目的 `backend/.venv/bin/python`，也可按本机开发说明选择外置缓存环境。调试配置不读取工作区 `.env`。

1. 打开 `backend/src/aftercare/business/service.py`，在 `create_case` 的第一条业务语句 `_require_role(user, "customer", "support")` 左侧设断点。
2. 在「运行和调试」选择「隔离建单调试」并启动。集成终端只显示本轮临时凭据的文件路径和 API 地址。这个入口以 `--editor` 使用普通业务服务，由 VS Code 接管断点。
3. 用「Tasks: Run Task」启动「启动隔离调试页面」。等本轮构建完成后打开 <http://127.0.0.1:15192>，按上方的建单步骤操作。请求命中断点后，在调用堆栈核对 `business/api.py` 到 `BusinessService.create_case`，然后继续。结束时先停止页面任务，再停止调试会话。若直接强制终止后遗留临时目录，按本轮终端路径核对归属后清理。
4. 默认使用 API `18092` 与页面 `15192`。仅调试 API 时可传 `--editor --port <空闲端口>`。联调页面仍需让其代理指向同一个 API 端口。现有终端 pdb 路径继续使用本页上方的无参数命令。正常退出会清理本轮临时数据库与凭据。

2026-11-10 使用已安装的 VS Code Python Debugger 扩展所带调试适配器，通过 DAP 在随机回环端口验证了编辑器模式。真实 HTTP 建单请求停在 `BusinessService.create_case` 第 367 行，继续后返回 201。本人回读 200，另一客户回读 404。临时凭据权限为 0600，退出后临时运行目录已清理。VS Code 图形界面和前端任务实操仍为 **NOT_RUN**。前一轮页面到 pdb 断点的实测记录独立保留。[调试适配器验收](../reports/vscode-debugpy-dap.json) · [既有页面与 pdb 实测](../reports/business-breakpoint-browser.json)

复跑适配器检查时，使用同一个锁定 Python 环境并传入本机 Python Debugger 扩展的 `bundled/libs` 路径。脚本选择空闲回环端口，验证实际断点和 HTTP 回读，退出时清理本轮临时目录。输出仅包含检查结果。

```sh
debug_python="$HOME/.cache/aftercare-agent/backend-venv/bin/python"
debugpy_lib="$HOME/.vscode/extensions/ms-python.debugpy-2026.6.0-darwin-arm64/bundled/libs"
"$debug_python" scripts/verify_debugpy_dap.py --debugpy-lib "$debugpy_lib"
```

### 根代理复验

根代理执行可复跑脚本，通过本机 debugpy 的真实 DAP 再次停到当前 `BusinessService.create_case`，继续后 201，本人读取 200、另一客户 404，临时凭据为 0600 且目录已清理。此次仍是调试协议与回环 HTTP 验证，不代表 VS Code GUI 点击启动或页面任务验收。[复验报告](../reports/vscode-debugpy-dap-root.json)
