# 本地备份与隔离恢复

在仓库根目录运行，使用已启动的项目 Docker Compose PostgreSQL 和 SeaweedFS。数据库命令在对应 PostgreSQL 容器内执行，使用容器已有的认证环境。工具优先探测 Compose 插件，并兼容独立的 `docker-compose`。项目支持备份 `app` 或 `test`，恢复一律进入本机测试集群的新数据库和新对象桶。

```bash
export PYTHONPATH=backend/src
backup_dir=".runtime/backup-test-$(date -u +%Y%m%dT%H%M%SZ)"
backend/.venv/bin/python -m aftercare.backup create --profile test --directory "$backup_dir"
backend/.venv/bin/python -m aftercare.backup verify --directory "$backup_dir"
backend/.venv/bin/python -m aftercare.backup restore --directory "$backup_dir"
```

首次使用时备份目录必须尚未创建，父目录必须已存在。工具创建权限为 0700 的目录和 0600 的文件。 `verify` 可离线执行，检查目录权限和所有权、清单 SHA-256、数据库归档格式及哈希、每个已登记对象的大小和哈希。校验码用于检测损坏，备份应来自可信的私有目录。数据库归档及图片可能包含敏感信息，文件权限属于本地保护边界。

成功恢复返回 `RESTORE_VERIFIED`、生成的 `ac_restore_<hex>` 数据库名及 `ac-restore-<hex>` 新桶。恢复前验证目标为本机 `127.0.0.1:55451` 的测试 PostgreSQL，连接用户为 `ac_test`，并比对集群身份和服务器编号。对象只写入测试 SeaweedFS 端点的生成新桶。工具保留原数据库、对象桶和 Redis。恢复阶段失败时，本次新建数据库可能只导入了一部分。工具将其标记为失败并保留可安全记录的目标名，方便定位中断位置。后续恢复会生成新目标。备份写入中断时，缺少完整清单和校验码的目录无法用于恢复。

## 一致性边界

PostgreSQL 在只读、可重复读事务中导出快照。 `pg_dump` 导入该快照，数据库归档和材料列表因此使用相同的数据库可见状态。数据库归档包含业务表、运行记录、LangGraph 检查点及其他项目表。清单额外记录关键订单、审批、支付账本、材料、运行、事件、模型尝试及检查点表的行数与内容指纹。当前公共表白名单包含入口模型审计、投递和限流、评测导入、Alembic 与 LangGraph 迁移记录。

新建备份使用 `aftercare-backup-v2`。清单按 schema 记录公共评测表及合法的 `eval_<32 位十六进制>` 隔离表。登记在隔离表中的附件以 `evaluation/<schema>/<object_key>` 在同一私有桶取回，恢复时写入相同键。同内容可共享一个归档字节文件，清单保留每条不同对象键和所属 schema。

历史 `aftercare-backup-v1` 仍可验证与恢复，返回的覆盖范围为 `public_only`。旧归档可能包含评测数据库行，旧清单只保证公共工单附件。v2 返回 `public_and_evaluation`，校验两类材料的数据库行与对象映射。旧 v2 清单可省略后来新增指纹的 `intake_model_audits`、`alembic_version` 和 `checkpoint_migrations`。已声明的指纹仍须匹配恢复结果。

材料协议先写入私有对象，再提交业务材料。备份按数据库快照内已提交的材料列表取对象，并验证数据库登记的大小与 SHA-256。快照后新上传的对象或未完成登记的暂存对象留在源桶。快照引用的对象缺失或内容改变会中断备份。恢复先验证归档和对象文件，随后逐步导入独立的新库，遇到错误立即停止。逐步提交可避免大量评测隔离表集中持有事务锁。导入完成后比对关键表的内容指纹与材料元数据，再写入新桶并逐个读回验证对象字节。全部核验完成才报告恢复成功。

快照包含当时已提交的运行和支付状态，包括暂停、在途及结果未知。Redis 队列消息、对象桶策略与外部服务状态独立于归档。做可重复恢复演练时，应暂停新的业务写入并让在途任务结束，再记录备份时间。恢复工具只核验隔离资源，并保持 Worker 停止在该恢复空间之外。主动续跑待执行运行或核对在途执行需要独立验收。

## 抽查订单、账本、图与图片

CLI 已比较源快照和恢复库的关键表指纹。可进一步使用受限连接助手核对业务数据，并验证通过恢复桶读取原材料键。

```python
from sqlalchemy import text

from aftercare.backup import restored_bucket_settings, restored_engine
from aftercare.business.storage import S3ObjectStore, verify_object
from aftercare.config import load_settings

settings = load_settings("test")
engine = restored_engine(settings, database_name)  # CLI 返回的数据库名
store = S3ObjectStore(restored_bucket_settings(settings, bucket_name))  # CLI 返回的桶名
try:
    with engine.connect() as conn:
        for table in ("business_orders", "business_executions", "checkpoints"):
            print(table, conn.scalar(text(f"SELECT count(*) FROM public.{table}")))
        image = conn.execute(text(
            "SELECT object_key, size, sha256 FROM public.business_materials LIMIT 1"
        )).one_or_none()
    if image:
        verify_object(store.get(image.object_key, image.size), image.size, image.sha256)
        print("material verified")
finally:
    engine.dispose()
```

2026-11-10 真实 PostgreSQL 与私有 S3 的 v2 恢复核验了 4,261 张表和 217 个对象。首次创建一致性快照时，评测隔离表和索引使 PostgreSQL 共享锁容量不足，返回 SQLSTATE 53200。保留原 test 数据卷，将 test 配置的 `max_locks_per_transaction` 调整为 512 后重新备份并恢复成功。当前受控测试另通过 31 项，覆盖文件权限、破坏检测、目标隔离、旧格式兼容、评测附件范围与新增表指纹。[创建失败日志](../reports/backup-current-create.log) · [恢复摘要](../reports/backup-current-v2-summary.json) · [恢复明细](../reports/backup-current-v2-restore.json) · [测试日志](../reports/backup-current-tests.log)
