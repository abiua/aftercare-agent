# P3 独立复核

2026-11-12。复核使用运行产物、固定源文件和实现代码，未另行启动模型或数据库。

## 持久化与重启

两台容器的 ID 和镜像 ID 在前后相同，Docker `StartedAt` 均向后推进。Milvus 从 `09:09:25.215186851Z` 变为 `09:10:31.025514369Z`，PostgreSQL 从 `09:09:25.15143778Z` 变为 `09:10:30.95884851Z`。原始日志记录了两个容器先停止再启动，之后通过健康检查。服务版本为 Milvus 2.6.9、PostgreSQL 16.12 和 pgvector 0.8.1。[生命周期报告](../reports/tau-p3-storage-resume.json) · [复跑日志](../.runtime/tau-rag-p3/lifecycle-resume.log)

同一组 384 维向量下，更新后的退货记录为 v2，重启后仍为 v2。被删除的发货记录在重启后仍为空。重新建表或建集合并写入原数据后，三条原记录、原 v1 内容和排名都恢复。独立重新计算四个阶段的两库排序和同 ID 分数差，排序完全一致，最大余弦差为 `9.0464e-8`。[生命周期报告](../reports/tau-p3-storage-resume.json)

首跑在两个容器均显示 Healthy 后，Milvus 对已持久化集合的首次读取仍返回 503，日志给出 `channel distribution is not serviceable`。复跑也记录到一次同类错误，采用有界读取重试后成功，报告记录 Milvus 1 次、pgvector 0 次重试。重启后的首次查询可用性和容器健康状态之间存在可观察的时间差。[首跑失败日志](../.runtime/tau-rag-p3/lifecycle-resume-first-failed.log) · [复跑日志](../.runtime/tau-rag-p3/lifecycle-resume.log)

正常入口另用两个持久化目录查询同一条英文退货问题。重启前后，每库读取相同的 77 块索引，最终 Top2 ID 和顺序完全相同。两个后端的 Top2 ID 也一致。[Milvus 重启前](../.runtime/tau-rag-p3/catalog-milvus-resume-before-query.json) · [Milvus 重启后](../.runtime/tau-rag-p3/catalog-milvus-resume-after-query.json) · [pgvector 重启前](../.runtime/tau-rag-p3/catalog-pgvector-resume-before-query.json) · [pgvector 重启后](../.runtime/tau-rag-p3/catalog-pgvector-resume-after-query.json)

## 同向量检索与引用

对照报告记录 450 字符窗口及 70 字符重叠，18 个固定窗口、77 个结构块。查询向量和两种切片的向量映射各有 SHA-256，同一映射直接传给两个数据库。复核根据固定政策重建全部切片，按保存的 ID 对 14 组配置的 252 条查询重新核对主引用、父级引用、完整条款命中、缺失行和聚合指标。84 个访问探针全部通过。两种切片分别在 Dense、RRF 和 RRF 加重排三组做两库逐查询比较，108/108 次 Top5 ID 和顺序一致。最终 Top5 内成对命中的稠密分数最大差为 `1.8368e-7`。[复跑两库对照](../reports/tau-p3-database-eval-resume-verified.json)

首次两库对照的全部查询和探针执行成功，但两个 Milvus 临时索引的清理报 `ConnectionNotExistException`。评测程序将其记录为清理失败，成功的查询分数和证据保留。同一地址的两个 Milvus 客户端原先共用 SDK 连接，关闭探针客户端使后续主索引清理失去连接。为每个客户端分配独立连接后，复跑使用相同的三个向量哈希，所有查询、探针与清理均成功。[首次两库对照](../reports/tau-p3-database-eval-resume.json) · [复跑两库对照](../reports/tau-p3-database-eval-resume-verified.json)

## 生成样例

Milvus 重排的退货样例生成调用成功，返回两段可定位原文。逐句核对，送达状态、确认订单号和商品、退款原支付方式或既有礼品卡、申请状态和邮件分别能在 118、120、124、126 行找到依据。该回答保留了退货退款时效未在政策给出的边界。召回 Top5 里也有取消和改支付方式的退款时效条款。回答没有把那些时效移用到退货。124 行来自父级上下文，按评测规则不计主引用命中。结论限于这一条生成样例。[生成报告](../reports/tau-p3-milvus-answer-resume.json)
