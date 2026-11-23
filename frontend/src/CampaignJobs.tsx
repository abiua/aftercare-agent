import { Alert, Button, Checkbox, Descriptions, Form, InputNumber, Select, Space } from 'antd';
import { useEffect, useState } from 'react';
import type { Client } from './api';
import { BenchmarkMetrics, type BenchmarkBatch } from './BenchmarkMetrics';
import { useActions, useResource } from './hooks';

type Catalog = {tasks: {id: string; label: string}[]; architectures: Record<string, string>; revision: string};
type Job = {
  id: string; created: string; commit: string; patched: boolean; snapshot: string; revision: string;
  task_ids: string[]; architectures: string[]; planned: number; finished: number;
  max_calls: number; max_usd: number; state: string; counts: Record<string, number>;
  billing?: {calls: number; known_usd: string; unsettled: number}; missing: string[]; message?: string; result?: BenchmarkBatch;
};
const states: Record<string, string> = {preview: '待确认', running: '运行中', completed: '已完成',
  partial: '部分完成', interrupted: '进程中断', failed: '启动或执行失败'};
const rowStates: Record<string, string> = {completed: '已完成', running: '处理中', error: '执行错误',
  unknown: '结果未知', timeout: '超时', budget_exhausted: '预算用尽', not_run: '未运行'};
const base = '/evaluation/campaign-jobs';

export function CampaignJobs({api}: {api: Client}) {
  const catalog = useResource<Catalog>(api, base + '/catalog');
  const jobs = useResource<{items: Job[]}>(api, base);
  const action = useActions(api);
  const [tasks, setTasks] = useState<string[]>([]);
  const [arms, setArms] = useState<string[]>(['S1']);
  const [usd, setUsd] = useState<number | null>(1);
  const [calls, setCalls] = useState<number | null>(50);
  const [selected, setSelected] = useState<Job>();
  const [confirmedBatch, setConfirmedBatch] = useState<string>();
  const items = jobs.value?.items ?? [];
  const stored = items.find(j => j.id === (selected?.id ?? new URLSearchParams(location.search).get("run_batch")));
  const job = (stored?.state === 'preview' && selected?.state === 'running' ? selected : stored) ?? selected ?? items[0];
  const consent = !!job && confirmedBatch === job.id;
  const active = job?.state === 'running' || items.some(j => j.state === 'running');
  const {refresh} = jobs;
  useEffect(() => {
    if (!active) return;
    const timer = setInterval(refresh, 2500);
    return () => clearInterval(timer);
  }, [active, refresh]);

  async function preview() {
    const value = await action.submit<Job>(base + '/preview',
      {task_ids: tasks, architectures: arms, max_usd: usd, max_calls: calls},
      {success: '已保存批次预览。', refresh});
    if (value) { setSelected(value); setConfirmedBatch(undefined); }
  }
  async function start() {
    if (!job || !consent) return;
    const value = await action.submit<Job>(base + '/' + job.id + '/start', {confirm_paid: true},
      {success: '已提交该批次。', refresh});
    if (value) { setSelected(value); setConfirmedBatch(undefined); }
  }
  return <div className="stack">
    <div><h2>配置当前版本评测</h2><p>在官方 Retail 测试集中选题。每个架构对每题运行一次，整个批次共用预算。</p></div>
    {catalog.error && <Alert type="error" title={catalog.error}/>}
    <Form layout="vertical" onFinish={preview}>
      <Form.Item label="评测任务" htmlFor="bench-tasks"><Select id="bench-tasks" mode="multiple"
        showSearch={{optionFilterProp: 'label'}} value={tasks} onChange={setTasks}
        placeholder="按题号或任务描述搜索" options={catalog.value?.tasks.map(t => ({value:t.id, label:'#' + t.id + ' ' + t.label}))}/></Form.Item>
      <Form.Item label="Agent 架构" htmlFor="bench-arms"><Select id="bench-arms" mode="multiple" value={arms} onChange={setArms}
        options={Object.entries(catalog.value?.architectures ?? {}).map(([value, name]) => ({value, label:value + ' · ' + name}))}/></Form.Item>
      <Space wrap align="start">
        <Form.Item label="整批美元上限" htmlFor="bench-usd"><InputNumber id="bench-usd" value={usd} onChange={setUsd}
          min={0.01} max={100} precision={2} step={0.1}/></Form.Item>
        <Form.Item label="整批模型调用上限" htmlFor="bench-calls"><InputNumber id="bench-calls" value={calls} onChange={setCalls}
          min={1} max={10000} precision={0}/></Form.Item>
      </Space>
      <div><Button htmlType="submit" loading={action.busy} disabled={!tasks.length || !arms.length || !usd || !calls}>生成免费预览</Button></div>
    </Form>
    {(action.error || jobs.error) && <Alert type="error" title={action.error || jobs.error}/>}
    {jobs.error && <Button onClick={refresh}>重新获取批次</Button>}
    {job && <section className="stack" aria-label="我的评测批次">
      <h2>我的评测批次</h2>
      <Select aria-label="选择我的批次" value={job.id} onChange={id => {setSelected(items.find(j => j.id === id)); setConfirmedBatch(undefined);}}
        options={(items.some(j => j.id === job.id) ? items : [job, ...items]).map(j => ({
          value:j.id, label:new Date(j.created).toLocaleString() + ' · ' + j.id.slice(0,8) + ' · ' + states[j.state]}))}/>
      <Descriptions size="small" column={{xs:1, sm:2}} items={[
        {key:'code', label:'被测代码', children:<span title={job.snapshot}>{job.commit.slice(0,12)}{job.patched && ' ＋ 工作区补丁'} · 快照 {job.snapshot.slice(0,12)}</span>},
        {key:'data', label:'数据集', children:'τ²-Bench Retail 测试集 · ' + job.revision.slice(0,12)},
        {key:'tasks', label:'本批题号', children:job.task_ids.join('、')},
        {key:'arms', label:'本批架构', children:job.architectures.join('、')},
        {key:'scope', label:'计划运行', children:job.task_ids.length + ' 题 × ' + job.architectures.length + ' 个架构 = ' + job.planned + ' 次'},
        {key:'budget', label:'预算上限', children:'$' + job.max_usd.toFixed(2) + ' · ' + job.max_calls + ' 次调用'},
        {key:'progress', label:'进度', children:states[job.state] + ' · ' + job.finished + '/' + job.planned},
      ]}/>
      {job.state !== 'preview' && job.state !== 'interrupted' && <a href={'?view=operations&batch='+encodeURIComponent(job.id)}>查看运行期间的系统指标</a>}
      {job.state === 'preview' && (job.missing.length ? <Alert type="warning" title={'运行环境缺少' + job.missing.join('、')}/> :
        <Space wrap><Checkbox checked={consent} onChange={e => setConfirmedBatch(e.target.checked ? job.id : undefined)}>确认按本批预算调用付费模型</Checkbox>
          <Button type="primary" disabled={!consent} loading={action.busy} onClick={start}>启动付费评测</Button></Space>)}
      {!!Object.keys(job.counts).length && <div>{Object.entries(job.counts).map(([s,n]) => (rowStates[s] ?? s) + ' ' + n).join(' · ')}</div>}
      {job.state === 'interrupted' && <Alert type="warning" title="评测进程已中断，已保存的进度可查看。本批不会自动重跑。"/>}
      {job.message && <Alert type="error" title={job.message}/>}
      {job.billing && <div>已记录 {job.billing.calls} 次调用 · 已核算 ${Number(job.billing.known_usd).toFixed(5)}
        {job.billing.unsettled > 0 && ` · 另有 ${job.billing.unsettled} 次费用待核清`}</div>}
      {job.result && <BenchmarkMetrics batch={job.result}/>}
    </section>}
  </div>;
}
