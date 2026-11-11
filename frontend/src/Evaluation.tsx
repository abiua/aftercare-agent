import { BlindReview } from './BlindReview';
import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Drawer, Form, Input, InputNumber, Modal, Progress, Select, Space, Table, Tabs, Tag } from 'antd';
import { DownloadOutlined, ExperimentOutlined, LinkOutlined, PlusOutlined, UploadOutlined } from '@ant-design/icons';
import { errorMessage, segment, type Client } from './api';
import type { Comparison, Dataset, EvalCase, EvalResult, EvaluationCapabilities, Experiment, ExperimentConfig, ImportedReport, Items, ReleasePolicy, Runtime } from './contracts';
import { useActions, useEventRefresh, useResource } from './hooks';
import { GateCriteria, ReleaseGate, ResultSummary } from './EvaluationEvidence';
import { EvaluationReviewReading } from './EvaluationReviewReading';
import { EvaluationReview } from './EvaluationReview';
import { evaluationUnsettled } from './evaluationProgress';
import { useEvaluationProgress, type EvaluationProgressStatus } from './useEvaluationProgress';
import { evaluationReason } from './evaluationMessages';
import { evaluationFeatures } from './evaluationPresentation';
import { ImportedReports, ReportImport, RetryExperiment, retryableStatuses } from './EvaluationOperations';
import { clearScenario, readEvaluationLocation, writeEvaluationLocation, type EvaluationLocation, type ScenarioLocation } from './evaluationRoute';
import { metricValue, OverallMetrics, ScenarioMetrics } from './ScenarioMetrics';
import { SupplementalUsage } from './SupplementalUsage';
import { Evidence, Feedback, intents, Json, label, Placeholder, Reload, Status, time } from './ui';

const base = '/evaluation';
const evidenceSources = ['fixture', 'live', 'fault', 'load'];
const pending = ['QUEUED', 'RUNNING'];

export function Evaluation({api, runtime}: {api: Client; runtime?: Runtime}) {
  const [route, setRoute] = useState(() => readEvaluationLocation(window.location.search));
  const capabilities = useResource<EvaluationCapabilities>(api, `${base}/capabilities`);
  const blindEnabled = !capabilities.error && capabilities.value?.chat_blind_review === true;
  const blindScope = route.tab === 'blind';
  const datasets = useResource<Items<Dataset>>(api, blindScope ? null : `${base}/datasets`);
  const experiments = useResource<Items<Experiment>>(api, blindScope ? null : `${base}/experiments`);
  const {importEnabled, retryEnabled, gatesEnabled} = evaluationFeatures(capabilities.value, runtime?.features);
  const imports = useResource<Items<ImportedReport>>(api, importEnabled && !blindScope ? `${base}/imports` : null);
  const policies = useResource<Items<ReleasePolicy>>(api, gatesEnabled && !blindScope ? `${base}/gates` : null);
  const [create, setCreate] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [openedImport, setOpenedImport] = useState<string>();
  const [lastImport, setLastImport] = useState<ImportedReport>();
  const [importNotice, setImportNotice] = useState('');
  const rows = experiments.value?.items ?? [];
  const datasetRows = datasets.value?.items ?? [];
  const catalogue = policies.value?.items ?? [];
  const refresh = experiments.refresh;
  const hasActive = rows.some((item) => pending.includes(item.status));

  useEffect(() => {
    const update = () => setRoute(readEvaluationLocation(window.location.search));
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  useEffect(() => {
    if (!hasActive) return;
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [hasActive, refresh]);

  function navigate(changes: Partial<EvaluationLocation>) {
    const next = {...route, ...changes};
    const href = writeEvaluationLocation(window.location.href, next);
    if (href !== window.location.href) history.pushState(null, '', href);
    setRoute(next);
  }

  function showExperiment(experiment?: string) {
    navigate({...clearScenario, experiment, caseId: undefined, repetition: undefined});
  }

  const tab = route.tab === 'imports' && !importEnabled ? 'experiments' : route.tab;
  return <div className="stack">
    <section className="hero">
      <div><div className="eyebrow">测试与评测 / 质量工作台</div><h1>测试中台</h1><p>检查每个案例的执行证据，复核失败，并对照模型与 Agent 配置。</p></div>
      {!blindScope && <Space wrap>
        <Button size="large" icon={<UploadOutlined />} disabled={!importEnabled} onClick={() => setImportOpen(true)}>导入评测报告</Button>
        <Button size="large" type="primary" icon={<PlusOutlined />} disabled={!datasetRows.length} onClick={() => setCreate(true)}>新建评测实验</Button>
      </Space>}
    </section>
    {!blindScope && !capabilities.loading && (!importEnabled || !retryEnabled || !gatesEnabled) &&
      <Alert type="info" showIcon title="部分评测操作当前不可用"
        description={`${[!importEnabled && '报告导入', !retryEnabled && '从原实验重跑', !gatesEnabled && '选择发布阈值'].filter(Boolean).join('、')}需评测服务启用支持。已有实验可继续查看。`} />}
    <Feedback error={datasets.error || experiments.error || policies.error} />
    <Card className="data-card">
      <Tabs activeKey={tab} onChange={(key) => navigate({...clearScenario, tab: key as EvaluationLocation['tab'], experiment: undefined, caseId: undefined, repetition: undefined})} items={[
        {key: 'cases', label: '用例库', children: !blindScope && <CaseLibrary api={api} datasets={datasetRows} />},
        {key: 'experiments', label: '实验与能力指标', children: !blindScope && <div className="stack">
          <div className="section-row"><div><h2>实验记录</h2><p className="muted">每次运行保留独立结果和配置。</p></div><Reload refresh={refresh} loading={experiments.loading} /></div>
          <Table rowKey="id" dataSource={rows} scroll={{x: 720}} loading={experiments.loading} columns={[
            {title: '实验名称', render: (_, item) => <Button type="link" className="cell-link" onClick={() => showExperiment(item.id)}>{item.name}</Button>},
            {title: '证据来源', render: (_, item) => <Tag>{label(item.config.source)}</Tag>},
            {title: '运行状态', render: (_, item) => <Status value={item.status} />},
            {title: '结束 / 计划', render: (_, item) => `${item.counts.finished} / ${item.counts.planned}`},
            {title: '开始时间', render: (_, item) => time(item.created_at)},
          ]} locale={{emptyText: <Placeholder text="暂无评测实验" />}} />
        </div>},
        {key: 'compare', label: '版本对照', children: !blindScope && <Compare api={api} experiments={rows} route={route}
          onSelection={navigate} onCase={(experiment, caseId, repetition) => navigate({...clearScenario, experiment, caseId, repetition})} />},
        ...(blindEnabled || blindScope ? [{key: 'blind', label: '独立盲审', children: blindScope && (blindEnabled ?
          <BlindReview api={api} /> : capabilities.error ?
            <Alert type="error" showIcon title="暂时无法检查盲审是否可用" description={capabilities.error}
              action={<Reload refresh={capabilities.refresh} loading={capabilities.loading} />} /> :
            capabilities.value ? <Alert type="info" showIcon title="当前环境未开放独立盲审" /> :
              <Placeholder text="正在检查独立盲审是否可用" />)}] : []),
        ...(importEnabled ? [{key: 'imports', label: '导入记录', children: !blindScope && <ImportedReports api={api} resource={imports}
          opened={openedImport} notice={importNotice} onOpen={(id) => {setOpenedImport(id); setImportNotice('');}} />}] : []),
      ]} />
    </Card>
    {!blindScope && create && <ExperimentEditor api={api} runtime={runtime} datasets={datasetRows} policies={catalogue}
      gatesEnabled={gatesEnabled} close={() => setCreate(false)}
      onCreated={(id) => {setCreate(false); navigate({...clearScenario, tab: 'experiments', experiment: id, caseId: undefined, repetition: undefined}); refresh();}} />}
    {!blindScope && importOpen && importEnabled && <ReportImport api={api} close={() => setImportOpen(false)}
      previous={[...(imports.value?.items ?? []), ...(lastImport ? [lastImport] : [])]}
      onImported={(record, notice) => {
        setImportOpen(false); setOpenedImport(record.id); setLastImport(record); setImportNotice(notice);
        navigate({...clearScenario, tab: 'imports', experiment: undefined, caseId: undefined, repetition: undefined}); imports.refresh();
      }} />}
    {!blindScope && route.experiment && <ExperimentDetails key={route.experiment} api={api} id={route.experiment}
      caseId={route.caseId} repetition={route.repetition} catalogue={catalogue} retryEnabled={retryEnabled}
      progressEnabled={capabilities.value ? capabilities.value.eval_progress_events === true : capabilities.error ? false : undefined}
      scenarioMetricsEnabled={route.tab === 'experiments' && capabilities.value?.eval_scenario_metrics === true}
      scenarioLocation={route} onScenarioSelection={navigate}
      supplementalUsageEnabled={capabilities.value?.eval_supplemental_usage === true}
      close={() => {showExperiment(); refresh();}} onChange={refresh}
      onCase={(caseId, repetition) => navigate({caseId, repetition})}
      onExperiment={(id) => {showExperiment(id); refresh();}} />}
  </div>;
}

function CaseLibrary({api, datasets}: {api: Client; datasets: Dataset[]}) {
  const [chosen, setChosen] = useState<string>();
  const [intent, setIntent] = useState<string>();
  const [scenario, setScenario] = useState<string>();
  const [split, setSplit] = useState<string>();
  const [difficulty, setDifficulty] = useState<string>();
  const [selected, setSelected] = useState<EvalCase>();
  const dataset = datasets.find((item) => item.id === chosen) ?? datasets[0];
  const query = dataset ? `${base}/cases?dataset=${segment(dataset.id)}` : null;
  const resource = useResource<Items<EvalCase>>(api, query);
  const cases = resource.value?.items ?? [];
  const options = (field: 'intent' | 'scenario' | 'difficulty' | 'split') => [...new Set(cases.map((item) => item[field]).filter(Boolean))]
    .map((value) => ({value, label: field === 'intent' ? intents[value as keyof typeof intents] ?? value : label(value)}));
  const rows = cases.filter((row) => (!intent || row.intent === intent) && (!scenario || row.scenario === scenario) && (!split || row.split === split) && (!difficulty || row.difficulty === difficulty));
  return <div className="stack">
    <div className="section-row"><div><h2>用例库</h2><p className="muted">查看用例输入、预期行为和人工标签。</p></div><Reload refresh={resource.refresh} loading={resource.loading} /></div>
    <Space wrap>
      <Select aria-label="数据集版本" className="filter-select wide" placeholder="数据集版本" value={dataset?.id} onChange={(id) => {
        setChosen(id); setIntent(undefined); setSplit(undefined); setScenario(undefined); setDifficulty(undefined);
      }} options={datasets.map((item) => ({value: item.id, label: `${item.name} · ${item.version}（${item.case_count} 案）`}))} />
      <Select aria-label="意图" className="filter-select" placeholder="售后类型" allowClear options={options('intent')} value={intent} onChange={setIntent} />
      <Select aria-label="异常类型" className="filter-select" placeholder="异常类型" allowClear options={options('scenario')} value={scenario} onChange={setScenario} />
      <Select aria-label="难度" className="filter-select" placeholder="难度" allowClear options={options('difficulty')} value={difficulty} onChange={setDifficulty} />
      <Select aria-label="划分" className="filter-select" placeholder="数据划分" allowClear options={options('split')} value={split} onChange={setSplit} />
    </Space>
    <Feedback error={resource.error} />
    <Table rowKey="id" dataSource={rows} scroll={{x: 700}} loading={resource.loading} columns={[
      {title: '案例编号', render: (_, item) => <Button type="link" className="cell-link" onClick={() => setSelected(item)}>{item.id}</Button>},
      {title: '售后类型', render: (_, item) => intents[item.intent] ?? item.intent},
      {title: '场景', dataIndex: 'scenario'},
      {title: '难度', render: (_, item) => label(item.difficulty)},
      {title: '划分', render: (_, item) => label(item.split)},
      {title: '核验状态', render: (_, item) => <Status value={item.review_status} />},
    ]} locale={{emptyText: <Placeholder text="暂无匹配用例" />}} />
    {selected && <Drawer title={`案例 ${selected.id}`} open onClose={() => setSelected(undefined)} size="large">
      <div className="stack">
        <Space wrap><Status value={selected.review_status} /><Tag>{label(selected.split)}</Tag><Tag>{intents[selected.intent] ?? selected.intent}</Tag></Space>
        <div className="compare-grid"><Card title="输入"><Json value={selected.input} /></Card><Card title="预期行为"><Json value={selected.expected} /></Card></div>
        <Evidence title="冻结订单与政策快照"><Json value={selected.snapshot} /></Evidence>
      </div>
    </Drawer>}
  </div>;
}

type ExperimentValues = ExperimentConfig & {name: string; case_ids?: string[]};
function ExperimentEditor({api, runtime, datasets, policies, gatesEnabled, close, onCreated}: {
  api: Client; runtime?: Runtime; datasets: Dataset[]; policies: ReleasePolicy[]; gatesEnabled: boolean;
  close: () => void; onCreated: (id: string) => void;
}) {
  const actions = useActions(api);
  const [form] = Form.useForm<ExperimentValues>();
  const source = Form.useWatch('source', form);
  const gatePolicy = Form.useWatch('gate_policy_id', form);
  const selectedPolicy = policies.find((policy) => policy.id === gatePolicy);
  const datasetId = Form.useWatch('dataset_id', form) ?? datasets[0]?.id;
  const split = Form.useWatch('split', form);
  const options = useResource<Items<EvalCase>>(api, datasetId ? `${base}/cases?dataset=${segment(datasetId)}` : null);
  const availableCases = options.value?.items ?? [];
  const splits = [...new Set(availableCases.map((item) => item.split))];
  const subset = availableCases.filter((item) => !split || item.split === split);
  const allowLive = runtime?.mode === 'live';
  return <Modal title="新建评测实验" open width={680} footer={null} onCancel={actions.busy ? undefined : close}>
    <Form form={form} layout="vertical" initialValues={{
      dataset_id: datasets[0]?.id, source: 'fixture', mode: 'single', strategy: 'routed', repetitions: 1, concurrency: 1,
      max_calls: 10, budget_usd: 0,
    }} onFinish={async (values) => {
      if (values.source === 'live' && !allowLive) return;
      const result = await actions.submit<Experiment>(`${base}/experiments`, {
        ...values, case_ids: values.case_ids?.length ? values.case_ids : undefined, name: values.name.trim(),
        gate_policy_id: gatesEnabled && selectedPolicy ? selectedPolicy.id : undefined,
      }, {success: '实验已提交'});
      if (result) onCreated(result.id);
    }}>
      <div className="form-grid">
        <Form.Item label="实验名称" name="name" rules={[{required: true, whitespace: true, message: '请输入名称'}]}><Input maxLength={120} placeholder="例如 固定模型比较单助手与三助手" /></Form.Item>
        <Form.Item label="数据集版本" name="dataset_id" rules={[{required: true}]}><Select onChange={() => {form.setFieldValue('split', undefined); form.setFieldValue('case_ids', undefined);}} options={datasets.map((item) => ({value: item.id, label: `${item.name} · ${item.version}`}))} /></Form.Item>
        <Form.Item label="数据划分" name="split"><Select allowClear placeholder="全部" onChange={() => form.setFieldValue('case_ids', undefined)} options={splits.map((value) => ({value, label: label(value)}))} /></Form.Item>
        <Form.Item label="证据来源" name="source" rules={[{required: true}]}><Select options={evidenceSources.map((value) => ({value, label: label(value), disabled: value === 'live' && !allowLive}))} /></Form.Item>
        <Form.Item label="Agent 配置" name="mode" rules={[{required: true}]}><Select options={['single', 'multi'].map((value) => ({value, label: label(value)}))} /></Form.Item>
        <Form.Item label="模型策略" name="strategy" rules={[{required: true}]}><Select options={['routed', 'economy', 'analysis'].map((value) => ({value, label: label(value)}))} /></Form.Item>
        <Form.Item label="每案重复次数" name="repetitions" rules={[{required: true, type: 'integer', min: 1, max: 3}]}><InputNumber min={1} max={3} precision={0} className="full" /></Form.Item>
        <Form.Item label="并发数" name="concurrency" rules={[{required: true, type: 'integer', min: 1, max: 4}]}><InputNumber min={1} max={4} precision={0} className="full" /></Form.Item>
        <Form.Item label="调用次数上限" name="max_calls" rules={[{required: true, type: 'integer', min: 1, max: 144}]}><InputNumber min={1} max={144} precision={0} className="full" /></Form.Item>
        <Form.Item label="费用上限（美元）" name="budget_usd" rules={[{required: true, type: 'number', min: source === 'live' ? 0.0001 : 0, max: 10}]}><InputNumber min={0} max={10} precision={4} className="full" /></Form.Item>
      </div>
      <Form.Item label="指定案例（可选）" name="case_ids" extra="留空将使用当前划分的全部案例。">
        <Select mode="multiple" allowClear loading={options.loading} options={subset.map((item) => ({value: item.id, label: `${item.id} · ${intents[item.intent] ?? item.intent}`}))} />
      </Form.Item>
      <Form.Item label="发布阈值（可选）" name="gate_policy_id"
        extra={gatesEnabled ? '阈值作为技术检查依据，发布仍需独立审批。' : '当前评测服务尚未开放此配置。'}>
        <Select allowClear disabled={!gatesEnabled || !policies.length}
          placeholder={gatesEnabled && !policies.length ? '暂无可用的阈值方案' : '选择阈值方案'}
          options={policies.map((policy) => ({value: policy.id, label: `${policy.name} · ${policy.version}`}))} />
      </Form.Item>
      {gatesEnabled && selectedPolicy && <GateCriteria policy={selectedPolicy} />}
      <Space orientation="vertical" className="full">
        <Feedback error={options.error} />
        {source === 'live' && <Alert type="warning" showIcon title="启动后可能产生真实模型费用。" />}
        <Feedback error={actions.error} />
        <Button type="primary" htmlType="submit" icon={<ExperimentOutlined />} loading={actions.busy} disabled={!!options.error || options.loading || !availableCases.length}>启动实验</Button>
      </Space>
    </Form>
  </Modal>;
}

function ExperimentDetails({api, id, caseId, repetition, catalogue, retryEnabled, supplementalUsageEnabled, scenarioMetricsEnabled, progressEnabled, scenarioLocation, onScenarioSelection, close, onChange, onCase, onExperiment}: {
  api: Client; id: string; caseId?: string; repetition?: number; catalogue: ReleasePolicy[]; retryEnabled: boolean;
  supplementalUsageEnabled: boolean; scenarioMetricsEnabled: boolean; progressEnabled?: boolean; scenarioLocation: ScenarioLocation;
  onScenarioSelection: (changes: ScenarioLocation) => void;
  close: () => void; onChange: () => void; onCase: (caseId?: string, repetition?: number) => void; onExperiment: (id: string) => void;
}) {
  const path = `${base}/experiments/${segment(id)}`;
  const resource = useResource<Experiment>(api, path);
  const [retry, setRetry] = useState(false);
  const [drawerReady, setDrawerReady] = useState(false);
  const [filter, setFilter] = useState<string>();
  const [intent, setIntent] = useState<string>();
  const [cancelVersion, setCancelVersion] = useState<number>();
  const [exportError, setExportError] = useState('');
  const [downloading, setDownloading] = useState<string>();
  const value = resource.value;
  const refresh = resource.refresh;
  const active = !!value && pending.includes(value.status);
  const unsettled = evaluationUnsettled(value);
  const refreshProgress = useEventRefresh(refresh);
  const refreshSettled = useCallback(() => {refreshProgress(); onChange();}, [refreshProgress, onChange]);
  const progress = useEvaluationProgress(api, progressEnabled && value ? `${path}/progress` : null,
    id, refreshProgress, refreshSettled, refresh);
  const poll = unsettled && (progressEnabled === false || progress === 'polling');
  useEffect(() => {
    if (!poll) return;
    const timer = setInterval(refresh, 4000);
    return () => clearInterval(timer);
  }, [poll, refresh]);

  async function download(format: 'json' | 'csv') {
    setDownloading(format);
    setExportError('');
    try {
      const blob = await api.blob(`${path}/export?format=${format}`);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url;
      a.download = `评测-${id}.${format}`;
      a.click();
      setTimeout(() => URL.revokeObjectURL(url), 1000);
    } catch (error) {
      setExportError(errorMessage(error));
    } finally {
      setDownloading(undefined);
    }
  }

  const result = value?.results.find((item) => item.case_id === caseId && item.repetition === repetition);
  const eligible = !!value && value.results.some((item) => retryableStatuses.includes(item.status));
  const rows = value?.results.filter((item) => (!filter || item.status === filter) && (!intent || item.intent === intent)) ?? [];
  return <Drawer title={value?.name ?? '评测实验'} open afterOpenChange={setDrawerReady} onClose={close} size="large" extra={<Reload refresh={refresh} loading={resource.loading} />}>
    <div className="stack">
      <Feedback error={resource.error || exportError} />
      {value && <>
        <div className="section-row">
          <Space wrap><Status value={value.status} /><Tag>{label(value.config.source)}</Tag><Tag>{label(value.config.mode)}</Tag><Tag>{label(value.config.strategy)}</Tag></Space>
          <Space wrap>
            <Button icon={<DownloadOutlined />} loading={downloading === 'json'} disabled={!!downloading} onClick={() => void download('json')}>导出 JSON</Button>
            <Button icon={<DownloadOutlined />} loading={downloading === 'csv'} disabled={!!downloading} onClick={() => void download('csv')}>导出 CSV</Button>
            {active && <Button danger onClick={() => setCancelVersion(value.version)}>取消实验</Button>}
            {!active && eligible && <Button disabled={!retryEnabled} onClick={() => setRetry(true)}>从原实验重跑</Button>}
          </Space>
        </div>
        {value.parent_experiment_id && <div><Button type="link" className="cell-link" onClick={() => onExperiment(value.parent_experiment_id!)}>查看关联原实验</Button></div>}
        {caseId && !result && <Alert type="warning" showIcon title="该链接对应的案例运行未找到。" />}
        <Card size="small" title="执行进度" extra={<EvaluationProgressBadge status={progress} polling={poll} />}>
          <Descriptions size="small" column={{xs: 2, sm: 4}} items={[
            {key: 'planned', label: '计划', children: value.counts.planned},
            {key: 'started', label: '已启动', children: value.counts.started},
            {key: 'finished', label: '已结束', children: value.counts.finished},
            {key: 'not_run', label: '未运行', children: value.counts.not_run},
            ...(value.counts.failed === undefined ? [] : [{key: 'failed', label: '失败', children: value.counts.failed}]),
            ...(value.counts.cancelled === undefined ? [] : [{key: 'cancelled', label: '已取消', children: value.counts.cancelled}]),
            ...(value.counts.timed_out === undefined ? [] : [{key: 'timed_out', label: '超时', children: value.counts.timed_out}]),
          ]} />
          {value.counts.planned > 0 && <Progress percent={Math.round(100 * value.counts.finished / value.counts.planned)} />}
          <Evidence title="冻结配置与预算"><Json value={value.config} /></Evidence>
        </Card>
        {scenarioMetricsEnabled ?
          <ScenarioMetrics api={api} experiment={value} location={scenarioLocation} onSelection={onScenarioSelection} onCase={onCase} /> :
          <OverallMetrics metrics={value.metrics} />}
        <ReleaseGate experiment={value} catalogue={catalogue} />
        <div className="section-row">
          <div><h3>案例运行与复核</h3><p className="muted">以下筛选只改变案例列表。</p></div>
          <Space wrap>
            <Select allowClear aria-label="运行结果" placeholder="运行结果" className="filter-select" options={[...new Set(value.results.map((row) => row.status))].map((status) => ({value: status, label: label(status)}))} value={filter} onChange={setFilter} />
            <Select allowClear aria-label="售后类型" placeholder="售后类型" className="filter-select" options={Object.entries(intents).map(([value, label]) => ({value, label}))} value={intent} onChange={setIntent} />
          </Space>
        </div>
        <Table rowKey="id" dataSource={rows} scroll={{x: 570}} columns={[
          {title: '案例', render: (_, row) => <Button type="link" className="cell-link" onClick={() => onCase(row.case_id, row.repetition)}>{row.case_id}</Button>},
          {title: '重复编号', dataIndex: 'repetition'},
          {title: '结果', render: (_, row) => <Status value={row.status} />},
          {title: '人工复核', render: (_, row) => row.reviews.length ? `${row.reviews.length} 条` : '待复核'},
          {title: '失败原因', render: (_, row) => evaluationReason(row.failure_reason) || '—'},
        ]} locale={{emptyText: <Placeholder text="暂无匹配的案例结果" />}} />
      </>}
    </div>
    {result && <ResultDetails key={result.id} api={api} result={result} supplementalUsageEnabled={supplementalUsageEnabled}
      parentReady={drawerReady} close={() => onCase()} refresh={refresh} />}
    {value && retry && retryEnabled && <RetryExperiment api={api} experiment={value} close={() => setRetry(false)}
      refresh={refresh} onCreated={(id) => {setRetry(false); onExperiment(id);}} />}
    {value && cancelVersion !== undefined && <CancelExperiment api={api} path={path} version={cancelVersion} currentVersion={value.version} active={active}
      close={() => setCancelVersion(undefined)} refresh={() => {refresh(); onChange();}} />}
  </Drawer>;
}

const progressLabels: Record<EvaluationProgressStatus, string> = {
  idle: '', connecting: '连接进度记录中', live: '实时更新', reconnecting: '正在恢复进度连接',
  polling: '定时更新', finished: '进度已结束', denied: '当前身份无法读取进度',
};

function EvaluationProgressBadge({status, polling}: {status: EvaluationProgressStatus; polling: boolean}) {
  const text = polling ? progressLabels.polling : status === 'polling' ? '' : progressLabels[status];
  return text ? <Tag color={status === 'live' ? 'green' : undefined} role="status">{text}</Tag> : null;
}

function CancelExperiment({api, path, version, currentVersion, active, close, refresh}: {
  api: Client; path: string; version: number; currentVersion: number; active: boolean; close: () => void; refresh: () => void;
}) {
  const actions = useActions(api);
  const stale = version !== currentVersion || !active;
  return <Modal title="取消评测实验" open onCancel={actions.busy ? undefined : close} footer={null}>
    <div className="stack">
      <p>取消后停止调度后续案例。已发出的调用、费用与已有结果会保留。</p>
      {stale && <Alert type="warning" showIcon title="实验进度已更新，请关闭后核对最新状态。" />}
      <Feedback error={actions.error} />
      <Button danger type="primary" disabled={stale} loading={actions.busy} onClick={async () => {
        const result = await actions.submit(`${path}/cancel`, {expected_version: version}, {success: '取消请求已提交', refresh});
        if (result) close();
      }}>确认取消</Button>
    </div>
  </Modal>;
}

function ResultDetails({api, result, supplementalUsageEnabled, parentReady, close, refresh}: {
  api: Client; result: EvalResult; supplementalUsageEnabled: boolean; parentReady: boolean; close: () => void; refresh: () => void;
}) {
  const [drawerReady, setDrawerReady] = useState(false);
  return <Drawer title={`案例 ${result.case_id} · 第 ${result.repetition} 次`} open afterOpenChange={setDrawerReady} onClose={close} size="large">
    <div className="stack">
      <div><Status value={result.status} /></div>
      <EvaluationReviewReading result={result} />
      <EvaluationReview api={api} result={result} refresh={refresh} ready={parentReady && drawerReady} />
      <Evidence title="查看程序检查、参考答案与调用费用">
        <ResultSummary result={result} />
        {supplementalUsageEnabled && <SupplementalUsage api={api} resultId={result.id} />}
      </Evidence>
    </div>
  </Drawer>;
}

function Compare({api, experiments, route, onSelection, onCase}: {
  api: Client; experiments: Experiment[]; route: EvaluationLocation;
  onSelection: (changes: Partial<EvaluationLocation>) => void;
  onCase: (experiment: string, caseId: string, repetition: number) => void;
}) {
  const {left, right} = route;
  const [difference, setDifference] = useState<string>();
  const distinct = !!left && !!right && left !== right;
  const comparison = useResource<Comparison>(api, distinct ? `${base}/compare?left=${segment(left)}&right=${segment(right)}` : null);
  const options = experiments.map((item) => ({value: item.id, label: `${item.name} · ${label(item.config.source)}`}));
  if (left && !options.some((item) => item.value === left)) options.push({value: left, label: '链接所选基线实验'});
  if (right && !options.some((item) => item.value === right)) options.push({value: right, label: '链接所选对照实验'});
  const data = comparison.value;
  const shareLink = distinct ? writeEvaluationLocation(window.location.href, {tab: 'compare', left, right}) : undefined;
  return <div className="stack">
    <div className="section-row">
      <div><h2>成对比较</h2><p className="muted">选择两个实验，对照同一案例的结果和质量、费用、耗时指标。</p></div>
      <Reload refresh={comparison.refresh} loading={comparison.loading} />
    </div>
    <div className="form-grid">
      <div><label className="field-label" htmlFor="compare-left">基线实验</label><Select id="compare-left" placeholder="选择基线实验" options={options} value={left} onChange={(left) => onSelection({left, experiment: undefined, caseId: undefined, repetition: undefined})} className="full" /></div>
      <div><label className="field-label" htmlFor="compare-right">对照实验</label><Select id="compare-right" placeholder="选择对照实验" options={options} value={right} onChange={(right) => onSelection({right, experiment: undefined, caseId: undefined, repetition: undefined})} className="full" /></div>
    </div>
    {shareLink && <Evidence title="分享当前对照"><Input prefix={<LinkOutlined />} aria-label="对照链接" readOnly value={shareLink} onFocus={(event) => event.currentTarget.select()} /></Evidence>}
    <Feedback error={comparison.error} />
    {!distinct && <Placeholder text="请选择两个不同的实验" />}
    {data && !data.compatible && <Alert type="warning" showIcon title="实验条件不可直接比较" description={data.reasons?.length ? data.reasons.join('；') : '请检查数据集、证据来源与冻结配置。'} />}
    {data?.compatible && <>
      {data.pairing && <Alert showIcon type={data.pairing.complete ? 'success' : 'info'}
        title={`已完成配对 ${data.pairing.finished} / ${data.pairing.planned}`}
        description={data.pairing.complete ? undefined : '全部计划案例完成配对后显示对照指标。'} />}
      <Table rowKey="key" size="small" dataSource={data.metrics ?? []} pagination={false} columns={[
        {title: '指标', dataIndex: 'label'},
        {title: '基线', render: (_, row) => <Space orientation="vertical" size={0}>
          {metricValue(row.left, row.unit)}{row.left_denominator != null && <span className="muted">样本量 {row.left_denominator}</span>}
        </Space>},
        {title: '对照', render: (_, row) => <Space orientation="vertical" size={0}>
          {metricValue(row.right, row.unit)}{row.right_denominator != null && <span className="muted">样本量 {row.right_denominator}</span>}
        </Space>},
      ]} locale={{emptyText: '暂无可比指标'}} />
      <div><Select className="filter-select" aria-label="筛选版本差异" placeholder="全部差异" allowClear
        value={difference} onChange={setDifference}
        options={['REGRESSION', 'IMPROVED', 'CHANGED', 'UNCHANGED', 'UNPAIRED'].map((value) => ({value, label: label(value)}))} /></div>
      <Table rowKey={(row) => `${row.case_id}:${row.repetition}`} size="small"
        dataSource={data.rows.filter((row) => !difference || row.change === difference)} scroll={{x: 650}} columns={[
        {title: '案例', dataIndex: 'case_id'},
        {title: '重复编号', dataIndex: 'repetition'},
        {title: '基线结果', render: (_, row) => <Status value={row.left_status} />},
        {title: '对照结果', render: (_, row) => <Status value={row.right_status} />},
        {title: '差异', render: (_, row) => <Status value={row.change} />},
        {title: '查看案例', render: (_, row) => <Space wrap>
          {left && <Button type="link" className="cell-link" onClick={() => onCase(left, row.case_id, row.repetition)}>基线详情</Button>}
          {right && <Button type="link" className="cell-link" onClick={() => onCase(right, row.case_id, row.repetition)}>对照详情</Button>}
        </Space>},
      ]} locale={{emptyText: '暂无可比案例'}} />
    </>}
  </div>;
}
