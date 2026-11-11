import { useCallback, useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Descriptions, Pagination, Select, Space, Table, Tag } from 'antd';
import { segment, type Client } from './api';
import type { ContributionBucket, Experiment, Metric, ScenarioContributors, ScenarioMetric, ScenarioScope, ScenarioSummary } from './contracts';
import type { ScenarioLocation } from './evaluationRoute';
import { useResource } from './hooks';
import { contributionBuckets, contributionReason, matchesContributors, matchesScenarioSummary, scenarioQuery, scenarioUnit } from './scenarioPresentation';
import { Evidence, Feedback, Json, Reload, Status } from './ui';

export function metricValue(value: number | null | undefined, unit: Metric['unit']) {
  if (value == null) return '未知';
  if (unit === 'ratio') return `${(value * 100).toFixed(1)}%`;
  if (unit === 'usd') return `$${value.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 6})}`;
  if (unit === 'ms') return `${value.toLocaleString('zh-CN')} ms`;
  return value.toLocaleString('zh-CN');
}

export function OverallMetrics({metrics}: {metrics: Metric[]}) {
  return <Card size="small" title="整体能力指标">
    <Table size="small" pagination={false} rowKey="key" dataSource={metrics} scroll={{x: 520}} columns={[
      {title: '指标', dataIndex: 'label'},
      {title: '结果', render: (_, row) => metricValue(row.value, row.unit)},
      {title: '分子 / 分母', render: (_, row) => `${row.numerator ?? '—'} / ${row.denominator ?? '—'}`},
      {title: '评分来源', dataIndex: 'source'},
    ]} locale={{emptyText: '暂无已计算指标'}} />
  </Card>;
}

export function ScenarioMetrics({api, experiment, location, onSelection, onCase}: {
  api: Client; experiment: Experiment; location: ScenarioLocation;
  onSelection: (changes: ScenarioLocation) => void; onCase: (caseId: string, repetition: number) => void;
}) {
  const scope: ScenarioScope = {scenario: location.scenario ?? null, intent: location.scenarioIntent ?? null};
  const query = scenarioQuery(scope);
  const path = `/evaluation/experiments/${segment(experiment.id)}/scenario-metrics`;
  const resource = useResource<ScenarioSummary>(api, `${path}${query.size ? `?${query}` : ''}`);
  const refresh = resource.refresh;
  const [invalidBasis, setInvalidBasis] = useState<string>();
  const [options, setOptions] = useState<Pick<ScenarioSummary, 'scenario_options' | 'intent_options'>>();
  const previousExperiment = useRef(experiment);
  const response = resource.value;
  const valid = response && matchesScenarioSummary(response, experiment.id, scope) ? response : undefined;
  const report = !resource.error && valid?.basis_revision !== invalidBasis ? valid : undefined;
  const selected = report?.metrics.find((metric) => metric.key === location.metric && metric.drilldown_supported);

  useEffect(() => {
    if (previousExperiment.current === experiment) return;
    previousExperiment.current = experiment;
    refresh();
  }, [experiment, refresh]);
  useEffect(() => {
    if (valid) setOptions({scenario_options: valid.scenario_options, intent_options: valid.intent_options});
  }, [valid]);
  const invalidate = useCallback((basis: string) => {
    setInvalidBasis(basis);
    refresh();
  }, [refresh]);

  const currentOptions = valid ?? options;
  const historical = report?.status === 'HISTORICAL_DEFINITION_UNAVAILABLE';
  return <Card size="small" title="能力指标" extra={<Reload refresh={refresh} loading={resource.loading} />}>
    <div className="stack">
      <Space wrap>
        <Select aria-label="指标场景" className="filter-select wide" disabled={historical} allowClear placeholder="全部场景"
          options={currentOptions?.scenario_options.map(({key, label}) => ({value: key, label}))}
          value={location.scenario} onChange={(scenario) => onSelection({scenario, metric: undefined})} />
        <Select aria-label="指标售后类型" className="filter-select" disabled={historical} allowClear placeholder="全部售后类型"
          options={currentOptions?.intent_options.map(({key, label}) => ({value: key, label}))}
          value={location.scenarioIntent} onChange={(scenarioIntent) => onSelection({scenarioIntent, metric: undefined})} />
        {(location.scenario || location.scenarioIntent) && <Button onClick={() =>
          onSelection({scenario: undefined, scenarioIntent: undefined, metric: undefined})}>重置指标范围</Button>}
      </Space>
      {invalidBasis && <Alert showIcon type="info" title="统计依据有更新"
        description={report ? '指标已更新，案例按同一依据读取。' : '旧统计已收起，正在核对更新后的记录。'} />}
      <Feedback error={resource.error || (response && !valid ? '服务返回的统计范围与所选范围不一致。请刷新后查看。' : '')} />
      {!report && resource.loading && <p role="status">正在读取当前范围的指标…</p>}
      {historical && <>
        <Alert showIcon type="warning" title="这份历史实验的指标定义暂无法重算"
          description={`${report.frozen_metrics_version ? '当前服务未提供对应的历史指标定义。' : '原实验未登记指标版本。'}下方整体汇总采用当前服务的计算规则。`} />
        <OverallMetrics metrics={experiment.metrics} />
      </>}
      {report && <Evidence title="查看指标定义版本">
        <Descriptions size="small" column={1} items={[
          {key: 'dataset', label: '数据集', children: report.dataset_id ?? '未登记'},
          {key: 'frozen', label: '实验冻结的定义版本', children: report.frozen_metrics_version ?? '未登记'},
          {key: 'computed', label: '当前服务的定义版本', children: report.computed_metrics_version},
        ]} />
      </Evidence>}
      {report?.status === 'AVAILABLE' && <>
        <Descriptions size="small" column={{xs: 2, sm: 3}} items={[
          {key: 'cases', label: '独立案例', children: report.scope_counts.unique_cases},
          {key: 'planned', label: '计划运行', children: report.scope_counts.planned},
          {key: 'repeats', label: '额外重复运行', children: report.scope_counts.repeated_runs},
          {key: 'started', label: '已启动', children: report.scope_counts.started},
          {key: 'finished', label: '已结束', children: report.scope_counts.finished},
          {key: 'not_run', label: '未运行', children: report.scope_counts.not_run},
        ]} />
        {!report.scope_counts.planned ? <p className="muted">当前范围没有计划运行的案例。</p> :
          <Table size="small" pagination={{pageSize: 12, hideOnSinglePage: true, showSizeChanger: false}} rowKey="key"
            dataSource={report.metrics} scroll={{x: 740}} columns={[
              {title: '指标', dataIndex: 'label'},
              {title: '结果', render: (_, row) => row.value == null ? '暂无可计算证据' : metricValue(row.value, row.unit)},
              {title: '计量依据', render: (_, row) => <MetricSamples metric={row} />},
              {title: '评分来源', dataIndex: 'source'},
              {title: '案例', render: (_, row) => row.drilldown_supported ?
                <Button type="link" className="cell-link" onClick={() => onSelection({metric: row.key})}
                  aria-label={`查看${row.label}的计入案例`}>查看案例</Button> : <span className="muted">暂不可下钻</span>},
            ]} locale={{emptyText: '暂无已计算指标'}} />}
        {location.metric && !selected && <Alert type="info" showIcon title="当前范围的这项指标没有可查看的计入案例。" />}
      </>}
      {report && selected && <Contributors key={`${report.basis_revision}:${selected.key}`} api={api} path={path} report={report}
        metric={selected} onCase={onCase} close={() => onSelection({metric: undefined})} invalidate={invalidate} />}
    </div>
  </Card>;
}

function MetricSamples({metric}: {metric: ScenarioMetric}) {
  const unit = scenarioUnit(metric.sample_unit);
  const unknown = metric.evidence_unknown ?? metric.unknown;
  return <div className="result-summary">
    {metric.denominator != null && <div>{metric.numerator != null ? `分子 ${metric.numerator} / ` : ''}分母 {metric.denominator}{metric.denominator_unit ? ` ${scenarioUnit(metric.denominator_unit)}` : ''}</div>}
    <div>样本 {metric.measured_samples} {unit} · 涉及 {metric.unique_cases} 个案例</div>
    <div>{unknown == null ? '未确定数量未登记' : `未确定 ${unknown} ${scenarioUnit(metric.unknown_unit)}`}</div>
    {metric.unknown_includes_measured === true && <div className="muted">未确定项也可能已计入样本和分母。</div>}
  </div>;
}

const pageSize = 20;

function Contributors({api, path, report, metric, onCase, close, invalidate}: {
  api: Client; path: string; report: ScenarioSummary; metric: ScenarioMetric;
  onCase: (caseId: string, repetition: number) => void; close: () => void; invalidate: (basis: string) => void;
}) {
  const [bucket, setBucket] = useState<ContributionBucket>();
  const [page, setPage] = useState(1);
  const offset = (page - 1) * pageSize;
  const query = scenarioQuery(report.scope);
  query.set('basis_revision', report.basis_revision);
  if (bucket) query.set('bucket', bucket);
  query.set('limit', String(pageSize));
  query.set('offset', String(offset));
  const resource = useResource<ScenarioContributors>(api, `${path}/${segment(metric.key)}/contributors?${query}`);
  const stale = resource.errorCode === 'METRIC_BASIS_CHANGED';
  const data = resource.value;
  const aligned = data && matchesContributors(data, report, metric.key, offset, pageSize);
  const value = aligned && !resource.error ? data : undefined;
  useEffect(() => {
    if (stale) invalidate(report.basis_revision);
  }, [stale, invalidate, report.basis_revision]);
  return <Card size="small" title={`${metric.label} · 计入与排除的案例`}
    extra={<Button onClick={close}>关闭指标案例</Button>}>
    <div className="stack">
      <div className="section-row">
        <Select aria-label="案例计入方式" className="filter-select" value={bucket} allowClear placeholder="全部案例"
          options={Object.entries(contributionBuckets).map(([key, label]) => ({value: key, label}))}
          onChange={(value) => {setBucket(value); setPage(1);}} />
        <Reload refresh={resource.refresh} loading={resource.loading} />
      </div>
      {value && <p className="muted">{Object.entries(contributionBuckets).map(([key, label]) =>
        `${label} ${value.bucket_counts[key as ContributionBucket]}`).join(' · ')}。同一运行可含多项证据。</p>}
      <Feedback error={resource.error || (data && !aligned ? '统计依据与当前指标不一致，请重新读取。' : '')} />
      {!value && resource.loading && <p role="status">正在读取计入案例…</p>}
      {value && <>
        <Table size="small" pagination={false} rowKey="result_id" dataSource={value.items} scroll={{x: 640}} columns={[
          {title: '案例与次数', render: (_, row) => <Button type="link" className="cell-link"
            onClick={() => onCase(row.case_id, row.repetition)}>{row.case_id} · 第 {row.repetition} 次</Button>},
          {title: '结果与复核', render: (_, row) => <div>
            <Status value={row.effective_status} />{row.machine_status !== row.effective_status &&
              <div>程序结果 <Status value={row.machine_status} /></div>}
            {row.review_version != null && <div className="muted">复核版本 {row.review_version}</div>}
          </div>},
          {title: '为何计入或排除', render: (_, row) => <div>
            <Space wrap>{[...new Set(row.contributions.flatMap((entry) => entry.measured ? [entry.bucket, 'included' as const] : [entry.bucket]))].map((kind) =>
              <Tag key={kind}>{contributionBuckets[kind]}</Tag>)}</Space>
            <ul>{row.contributions.slice(0, 4).map((entry, index) => <li key={index}>
              {contributionReason(entry.reason)}
              {entry.denominator != null && `，分母计入 ${entry.denominator}`}
              {entry.numerator != null && `，分子计入 ${entry.numerator}`}
              {entry.value != null && `，记录值 ${metricValue(entry.value, metric.unit)}`}
            </li>)}</ul>
            {row.contributions.length > 4 && <p className="muted">其余 {row.contributions.length - 4} 项见原始统计依据。</p>}
            <Evidence title="查看原始统计依据"><Json value={row.contributions} /></Evidence>
          </div>},
        ]} locale={{emptyText: '当前筛选没有对应案例。'}} />
        {value.total > pageSize && <Pagination size="small" current={page} pageSize={pageSize}
          total={value.total} showSizeChanger={false} onChange={setPage} />}
      </>}
    </div>
  </Card>;
}
