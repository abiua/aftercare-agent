import { Alert, Button, Descriptions, Empty, Space, Table, Tag } from 'antd';
import { useEffect, useState } from 'react';
import { errorMessage, type Client } from './api';
import { useResource } from './hooks';
import { Feedback, Reload } from './ui';

const arms = {
  strong_single: '强单 Agent',
  staged_single: '单 Agent 三阶段',
  multi_agent: '多 Agent',
};
const statuses = {
  completed: '已完成',
  error: '运行异常',
  timeout: '超时',
  running: '进行中',
  budget_exhausted: '预算用尽',
  unknown: '待核实',
  not_run: '未运行',
};
type Arm = keyof typeof arms;
type Cost = number | string | null;
type Usage = {
  agent_calls: number;
  user_calls: number;
  review_calls: number;
  agent_cost_usd: Cost;
  user_cost_usd: Cost;
  review_cost_usd: Cost;
};
type Group = Usage & {
  architecture: Arm;
  scheduled: number;
  completed: number;
  passed: number;
  unscored: number;
  duration_seconds: number | null;
};
type Attempt = Usage & {
  status: keyof typeof statuses;
  official_reward: number | null;
};
type Row = Attempt & {
  task_id: string;
  architecture: Arm;
  seed: number;
  task_summary: string | null;
  attempt_count: 1 | 2 | null;
  score_source: 'original' | 'technical_retry' | 'unscored' | null;
  initial_status: keyof typeof statuses | null;
  original_attempt: Attempt | null;
  repair_attempt: Attempt | null;
};
type Campaign = {
  scope: 'smoke' | 'full' | 'repair';
  revision: string;
  agent_model: string;
  user_model: string;
  review_model: string | null;
  task_ids: string[];
  trials_per_task: number;
  complete: boolean;
  all_scored: boolean | null;
  architectures: Group[];
  original_directory: string | null;
  repair_directory: string | null;
  original_architectures: Group[] | null;
  billing: {known_usd: Cost; unsettled_calls: number; unsettled_reserved_usd: Cost} | null;
  rows: Row[];
  tool_names: string[];
  budget: {
    max_agent_calls_per_run: number | null;
    max_user_calls_per_run: number | null;
    max_review_calls_per_run: number | null;
    max_output_tokens_per_call: number | null;
    max_steps: number | null;
    timeout_seconds_per_run: number | null;
    max_calls: number | null;
    max_usd: Cost;
  } | null;
};

function cost(value: Cost) {
  return value === null || !Number.isFinite(Number(value)) ? '未核清' : `$${value}`;
}

function totalCost(group: Group) {
  const values = [group.agent_cost_usd, group.user_cost_usd, group.review_cost_usd];
  if (values.some(value => value === null || !Number.isFinite(Number(value)))) return '未核清';
  return cost(Number(values.reduce<number>((sum, value) => sum + Number(value), 0).toFixed(8)));
}

function duration(seconds: number | null) {
  if (seconds == null) return '—';
  return seconds < 60 ? `${seconds.toFixed(1)} 秒` : `${(seconds / 60).toFixed(1)} 分钟`;
}

export function InvestigationCampaigns({api}: {api: Client}) {
  const [download, setDownload] = useState<'primary' | 'repair'>();
  const [downloadError, setDownloadError] = useState('');
  const {value, error, loading, refresh} = useResource<{items: Campaign[]}>(api, '/evaluation/campaigns');
  useEffect(() => {
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh]);
  const report = value?.items[0];
  const budget = report?.budget;
  const repairs = report?.rows.filter(row => row.attempt_count === 2) ?? [];

  async function downloadReport(batch: 'primary' | 'repair') {
    setDownload(batch);
    setDownloadError('');
    try {
      const blob = await api.blob(`/evaluation/campaigns/tau-official-${batch}`);
      const url = URL.createObjectURL(blob);
      const link = document.createElement('a');
      link.href = url;
      link.download = `tau-official-${batch}.json`;
      link.click();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (failure) {
      setDownloadError(errorMessage(failure));
    } finally {
      setDownload(undefined);
    }
  }

  return <div className="stack">
    <Alert showIcon type="info" title="τ²-Bench Retail 官方测试集" description="官方得 1 分算成功。失败、未完成和未运行的任务仍计入总数。"/>
    <div className="section-row"><Reload refresh={refresh} loading={loading}/></div>
    <Feedback error={error || downloadError}/>
    {!report && !loading && !error && <Empty description="尚无已发布的官方评测报告"/>}
    {report && <>
      {repairs.length > 0 && <Alert showIcon type="warning"
        title={`含 ${repairs.length} 条 API 异常后的补跑，首次异常记录保留`}
        description="只补跑未取得分数的 API 异常，费用包含首次尝试。已评分的任务不重跑。"/>}
      {repairs.length > 0 && <Space wrap>
        <Button onClick={() => void downloadReport('primary')} loading={download === 'primary'} disabled={download === 'repair'}>下载首次运行报告</Button>
        <Button onClick={() => void downloadReport('repair')} loading={download === 'repair'} disabled={download === 'primary'}>下载补跑报告</Button>
      </Space>}
      <Descriptions column={2} items={[
        {key:'scope', label:'样本', children:`${report.scope === 'full' ? '完整测试集' : '小样本验证'} · ${report.task_ids.length} 道任务 · 每组每题 ${report.trials_per_task} 次`},
        {key:'progress', label:'批次状态', children:report.complete ? (report.all_scored === false ? '已结束，部分任务未评分' : '已结束') : '结果未齐'},
        {key:'agent', label:'三组共用的 Agent 模型', children:report.agent_model},
        ...(report.billing ? [{key:'known', label:'已记录费用', children:cost(report.billing.known_usd)}] : []),
      ]}/>
      <details><summary>查看模型、预算和版本</summary><Descriptions column={1} items={[
        ...(report.original_directory && report.repair_directory ? [
          {key:'batches', label:'首次运行和补跑批次', children:`${report.original_directory} / ${report.repair_directory}`},
        ] : []),
        ...(report.billing ? [
          {key:'unsettled', label:'待核实请求', children:`${report.billing.unsettled_calls} 次 · 预留 ${cost(report.billing.unsettled_reserved_usd)}`},
        ] : []),
        {key:'revision', label:'官方版本', children:report.revision},
        {key:'user', label:'模拟客户模型', children:report.user_model},
        {key:'review', label:'评分模型', children:report.review_model ?? '报告未记录'},
        {key:'tools', label:'共用工具', children:report.tool_names.join('、') || '报告未记录'},
        {key:'calls', label:'单次运行调用上限', children:budget ? `Agent ${budget.max_agent_calls_per_run ?? '未记录'} / 模拟客户 ${budget.max_user_calls_per_run ?? '未记录'} / 评分 ${budget.max_review_calls_per_run ?? '未记录'}` : '报告未记录'},
        {key:'limits', label:'单次运行限制', children:budget && `${budget.max_steps ?? '未记录'} 步 / ${budget.timeout_seconds_per_run ?? '未记录'} 秒 / 每次调用最多 ${budget.max_output_tokens_per_call ?? '未记录'} 个输出 token`},
        {key:'batch', label:'整批模型调用上限', children:budget?.max_calls ?? '报告未记录'},
        {key:'cost', label:'整批费用上限', children:budget?.max_usd != null ? cost(budget.max_usd) : '报告未记录'},
      ]}/></details>
      <Table<Group> rowKey="architecture" dataSource={report.architectures} pagination={false} scroll={{x:1150}} columns={[
        {title:'调查方式', render:(_, g) => arms[g.architecture]},
        {title:report.complete ? '官方成功率' : '当前官方成功率', render:(_, g) => g.scheduled ? `${(100 * g.passed / g.scheduled).toFixed(1)}%（${g.passed}/${g.scheduled}）` : '—'},
        {title:'完成 / 计划', render:(_, g) => `${g.completed} / ${g.scheduled}`},
        {title:'完成但未成功', render:(_, g) => report.rows.filter(r => r.architecture === g.architecture && r.status === 'completed' && r.official_reward !== null && r.official_reward < 1).length},
        {title:'异常 / 待核实 / 进行中 / 未跑', render:(_, g) => {
          const rows = report.rows.filter(r => r.architecture === g.architecture);
          return `${rows.filter(r => r.status === 'error' || r.status === 'timeout' || r.status === 'budget_exhausted').length} / ${rows.filter(r => r.status === 'unknown').length} / ${rows.filter(r => r.status === 'running').length} / ${rows.filter(r => r.status === 'not_run').length}`;
        }},
        {title:'Agent 次数 / 费用', render:(_, g) => `${g.agent_calls} / ${cost(g.agent_cost_usd)}`},
        {title:'模拟客户次数 / 费用', render:(_, g) => `${g.user_calls} / ${cost(g.user_cost_usd)}`},
        {title:'评分模型次数 / 费用', render:(_, g) => `${g.review_calls} / ${cost(g.review_cost_usd)}`},
        {title:'费用小计', render:(_, g) => totalCost(g)},
        {title:'累计运行耗时', render:(_, g) => duration(g.duration_seconds)},
      ]}/>
      {repairs.length > 0 && report.original_architectures && <details>
        <summary>首次运行成绩</summary>
        <ul>{report.original_architectures.map(group => <li key={group.architecture}>
          {arms[group.architecture]}，成功 {group.passed}/{group.scheduled}，未评分 {group.unscored}
        </li>)}</ul>
      </details>}
      <Table<Row> rowKey={r => `${r.architecture}-${r.task_id}-${r.seed}`} dataSource={report.rows} scroll={{x:1000}} columns={[
        {title:'官方任务 ID', dataIndex:'task_id'},
        {title:'客户问题摘要', render:(_, r) => r.task_summary || '报告未提供'},
        {title:'调查方式', render:(_, r) => arms[r.architecture]},
        {title:'运行状态', render:(_, r) => <>
          <Tag>{statuses[r.status]}</Tag>
          {r.attempt_count === 2 && <Tag color="gold">首次{statuses[r.original_attempt?.status ?? r.initial_status ?? 'unknown']} · 第 2 次尝试</Tag>}
        </>},
        {title:'官方奖励', render:(_, r) => r.official_reward ?? '—'},
        {title:'Agent 次数 / 费用', render:(_, r) => `${r.agent_calls} / ${cost(r.agent_cost_usd)}`},
        {title:'模拟客户次数 / 费用', render:(_, r) => `${r.user_calls} / ${cost(r.user_cost_usd)}`},
        {title:'评分模型次数 / 费用', render:(_, r) => `${r.review_calls} / ${cost(r.review_cost_usd)}`},
      ]}/>
    </>}
  </div>;
}
