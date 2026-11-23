import { Descriptions, Select, Table } from 'antd';

type Group = {
  architecture: string; scheduled: number; passed: number;
  attempts: number; traces: number; partial: number; tools: number; errors: number; unknown_tools: number;
  steps_n: number; avg_steps: number | null; latency_n: number; p95_seconds: number | null;
  calls: number; priced: number; metered: number; ledger_gaps: number;
  input_tokens: number; output_tokens: number; cached_input_tokens: number; cost_per_success: number | null;
};

export type BenchmarkBatch = {
  id: string; label: string; note: string; revision: string; split: 'train' | 'test'; tasks: number;
  trials: number; commit: string | null; kernel: string | null; patched: boolean; adapter: string | null; snapshot?: string | null;
  repairs: number; groups: Group[];
};

const names: Record<string, string> = {
  S1: '强单 Agent', S1c: '低成本单 Agent', S2: '强单＋自检', M1: '全强多 Agent',
  M2: '低成本执行＋强审查', M2x: '仅审查', M2r: '仅专家',
  strong_single: '强单 Agent', staged_single: '单 Agent 三阶段', multi_agent: '多 Agent',
};
const number = new Intl.NumberFormat('zh-CN');
const percent = (n: number, d: number) => d ? (100 * n / d).toFixed(1) + '%' : '无分母';
const measured = (n: number, total: number) => n === total ? `${n} 题` : `覆盖 ${n}/${total} 题`;

export function BenchmarkMetrics({batch, batches, select}: {
  batch: BenchmarkBatch; batches: BenchmarkBatch[]; select: (id: string) => void;
}) {
  return <section aria-label="评测批次指标" className="stack">
    <div className="section-row"><h2>评测批次</h2>
      <Select aria-label="选择评测批次" value={batch.id} onChange={select}
        style={{width: 300, maxWidth: '100%'}} options={batches.map(b => ({value: b.id, label: b.label}))}/>
    </div>
    <Descriptions size="small" column={{xs: 1, sm: 2, md: 2}} items={[
      {key: 'code', label: '被测项目版本', children: <span title={batch.commit ?? undefined}>
        {batch.kernel && `${batch.kernel} · `}{batch.commit?.slice(0, 12) ?? '提交未记录'}{batch.patched && ' ＋ 工作区补丁'}
      </span>},
      {key: 'dataset', label: '评测数据集', children: <span title={batch.revision}>τ²-Bench Retail · {batch.split === 'train' ? '训练集' : '测试集'} · {batch.revision.slice(0, 12)}</span>},
      {key: 'tasks', label: '运行规模', children: `${batch.tasks} 题 · 每组每题 ${batch.trials} 次 · ${batch.repairs} 次补跑`},
      {key: 'scope', label: '批次说明', children: batch.note},
    ]}/>
    <Table<Group> aria-label="各架构评测指标" rowKey="architecture" dataSource={batch.groups}
      pagination={false} size="small" scroll={{x: 1060}} columns={[
        {title: '架构', fixed: 'left', width: 136, render: (_, g) => <><strong>{g.architecture}</strong><small>{names[g.architecture]}</small></>},
        {title: '任务成功率', render: (_, g) => <>{percent(g.passed, g.scheduled)}<small>{g.passed}/{g.scheduled} 题</small></>},
        {title: '工具调用失败率', render: (_, g) => <>{g.traces ? percent(g.errors, g.tools) : '缺少轨迹'}<small>{g.errors}/{g.tools} 次 · 轨迹 {g.traces}/{g.attempts}</small>
          {g.partial > 0 && <small>含 {g.partial} 条中断轨迹</small>}{g.unknown_tools > 0 && <small>{g.unknown_tools} 次结果未知</small>}</>},
        {title: '平均执行步数', render: (_, g) => <>{g.avg_steps?.toFixed(2) ?? '缺少轨迹'}<small>{measured(g.steps_n, g.scheduled)}</small></>},
        {title: '任务 P95 延迟', render: (_, g) => <>{g.p95_seconds == null ? '缺少耗时' : g.p95_seconds.toFixed(2) + ' 秒'}<small>{measured(g.latency_n, g.scheduled)}</small></>},
        {title: '实际 Token 消耗', render: (_, g) => <>{g.metered ? number.format(g.input_tokens + g.output_tokens) : '缺少回执'}
          {g.metered > 0 && <><small>输入 {number.format(g.input_tokens)} · 输出 {number.format(g.output_tokens)}</small><small>其中缓存输入 {number.format(g.cached_input_tokens)}</small></>}
          {(g.metered !== g.calls || g.ledger_gaps > 0) && <small>仅已知用量 · 回执 {g.metered}/{g.calls}{g.ledger_gaps > 0 && ' · 调用记录不齐'}</small>}</>},
        {title: '每成功一题成本', render: (_, g) => <>{g.cost_per_success == null ? (g.passed ? '费用未核清' : '无成功题') : '$' + g.cost_per_success.toFixed(5)}
          <small>{g.priced}/{g.calls} 次已核算{g.ledger_gaps > 0 && ' · 调用记录不齐'}</small></>},
      ]}/>
    <details><summary>指标口径与版本依据</summary>
      <ul>
        <li>成功率以官方得分为 1 的题数除以计划题数。工具失败率统计公开轨迹中助手实际执行且有明确结果的业务工具调用。</li>
        <li>一步指一个公开助手回合。每题累加首次与补跑的步数，缺少完整轨迹的题从均值中剔除。</li>
        <li>P95 按题累加所有尝试的运行秒数，在耗时齐全的题中取最近秩第 95 百分位。</li>
        <li>Token 和美元覆盖 Agent、模拟客户、评分者及补跑。每成功一题成本以全组费用除以成功题数，按回执用量和批次单价估算。</li>
        {batch.snapshot && <li>被测源码文件哈希清单指纹：<code style={{overflowWrap: 'anywhere'}}>{batch.snapshot}</code></li>}
        {batch.adapter && <li>被测适配器 SHA-256：<code style={{overflowWrap: 'anywhere'}}>{batch.adapter}</code></li>}
      </ul>
    </details>
  </section>;
}
