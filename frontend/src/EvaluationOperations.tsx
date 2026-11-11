import { useState } from 'react';
import { Alert, Button, Card, Drawer, Form, Input, Modal, Select, Space, Table, Tag } from 'antd';
import type { Client } from './api';
import { segment } from './api';
import type { Experiment, ImportedReport, Items } from './contracts';
import { useActions, useResource } from './hooks';
import { maxReportBytes, prepareReportImport, reportImportNotice } from './evaluationImport';
import { Evidence, Feedback, Json, label, Placeholder, Reload, Status, time } from './ui';

const base = '/evaluation';
export const retryableStatuses = ['FAILED', 'TIMEOUT', 'ERROR', 'NEEDS_REVIEW', 'NOT_RUN'];

export function ReportImport({api, previous, close, onImported}: {
  api: Client; previous: ImportedReport[]; close: () => void;
  onImported: (record: ImportedReport, notice: string) => void;
}) {
  const [file, setFile] = useState<File>();
  const [reading, setReading] = useState(false);
  const [validation, setValidation] = useState('');
  const actions = useActions(api);
  const busy = reading || actions.busy;

  return <Modal title="导入评测报告" open footer={null} onCancel={busy ? undefined : close}>
    <Form layout="vertical" onFinish={async ({origin}: {origin: string}) => {
      if (!file || busy) return;
      setValidation('');
      if (file.size > maxReportBytes) {setValidation('请选择 8 MB 以内的 JSON 文件。'); return;}
      setReading(true);
      try {
        const payload = await prepareReportImport(await file.arrayBuffer(), origin);
        const result = await actions.submit<ImportedReport>(`${base}/imports`, payload,
          {success: '', fingerprint: `${payload.sha256}:${payload.origin}`});
        if (result) onImported(result, reportImportNotice(payload.sha256, previous, result));
      } catch {
        setValidation('无法读取报告，请选择有效的 UTF-8 JSON 文件。');
      } finally {
        setReading(false);
      }
    }}>
      <div className="stack">
        <Alert type="info" showIcon title="导入报告会保存为待核验的独立证据。" description="评测运行、成对对照和发布门槛继续使用系统内的实验记录。" />
        <Form.Item label="报告文件" htmlFor="report-file" required extra="支持 UTF-8 JSON，最大 8 MB。">
          <input id="report-file" type="file" accept=".json,application/json" disabled={busy} onChange={(event) => {setFile(event.currentTarget.files?.[0]); setValidation('');}} />
        </Form.Item>
        <Form.Item label="报告来源" name="origin" rules={[{required: true, whitespace: true, max: 200, message: '请填写报告来源'}]}>
          <Input maxLength={200} disabled={busy} placeholder="例如 离线运行导出的评测报告" />
        </Form.Item>
        <Feedback error={validation || actions.error} />
        <div><Button type="primary" htmlType="submit" loading={busy} disabled={!file}>保存导入记录</Button></div>
      </div>
    </Form>
  </Modal>;
}

export function ImportedReports({api, resource, opened, notice, onOpen}: {
  api: Client; resource: ReturnType<typeof useResource<Items<ImportedReport>>>; opened?: string;
  notice: string; onOpen: (id?: string) => void;
}) {
  const detail = useResource<ImportedReport>(api, opened ? `${base}/imports/${segment(opened)}` : null);
  const record = detail.value;
  return <div className="stack">
    <div className="section-row">
      <div><h2>导入记录</h2><p className="muted">这些报告由评测人员留档，来源和内容待独立核验。</p></div>
      <Reload refresh={resource.refresh} loading={resource.loading} />
    </div>
    <Feedback error={resource.error} />
    <Table rowKey="id" dataSource={resource.value?.items ?? []} loading={resource.loading} scroll={{x: 540}} columns={[
      {title: '报告', render: (_, row) => <Button type="link" className="cell-link" onClick={() => onOpen(row.id)}>{row.name}</Button>},
      {title: '来源', dataIndex: 'origin'},
      {title: '导入时间', render: (_, row) => time(row.created_at)},
      {title: '证据状态', render: () => <Tag color="gold">待核验</Tag>},
    ]} locale={{emptyText: <Placeholder text="暂无导入报告" />}} />
    {!!opened && <Drawer open title={record?.name ?? '导入报告'} size="large" onClose={() => onOpen()}>
      <div className="stack">
        <Feedback error={detail.error} notice={notice} />
        {record && <>
          <Alert type="warning" showIcon title="导入报告待核验" description={`提供的报告来源：${record.origin}`} />
          <Card size="small" title="原报告自报结果"><Space wrap>
            <Status value={record.status} /><span>已结束 {record.counts?.finished ?? '未知'}／计划 {record.counts?.planned ?? '未知'}</span>
          </Space></Card>
          <Evidence title="展开原始报告和校验信息"><Json value={{sha256: record.sha256, report: record.report}} /></Evidence>
        </>}
      </div>
    </Drawer>}
  </div>;
}

export function RetryExperiment({api, experiment, close, refresh, onCreated}: {
  api: Client; experiment: Experiment; close: () => void; refresh: () => void; onCreated: (id: string) => void;
}) {
  const actions = useActions(api);
  const eligible = experiment.results.filter((item) => retryableStatuses.includes(item.status));
  const [version] = useState(experiment.version);
  const stale = experiment.version !== version;
  return <Modal title="从原实验重跑案例" open footer={null} onCancel={actions.busy ? undefined : close}>
    <Form layout="vertical" onFinish={async (values: {result_ids: string[]; name?: string}) => {
      if (stale) return;
      const result = await actions.submit<Experiment>(`${base}/experiments/${segment(experiment.id)}/retry`,
        {...values, name: values.name?.trim() || undefined, expected_version: version},
        {success: '新实验已提交，原实验结果已保留。', refresh});
      if (result) onCreated(result.id);
    }}>
      <div className="stack">
        <p>所选案例会进入新的关联实验，原实验及人工复核历史会保留。</p>
        {experiment.config.source === 'live' && <Alert type="warning" showIcon title="重跑可能产生真实模型费用。" />}
        {stale && <Alert type="warning" showIcon title="原实验状态已更新，请关闭后查看最新结果。" />}
        <Form.Item label="重跑案例" name="result_ids" rules={[{required: true, type: 'array', min: 1, message: '请选择至少一个案例'}]}>
          <Select mode="multiple" options={eligible.map((item) => ({
            value: item.id, label: `${item.case_id} · 第 ${item.repetition} 次 · ${label(item.status)}`,
          }))} />
        </Form.Item>
        <Form.Item label="新实验名称（可选）" name="name"><Input maxLength={120} placeholder="留空使用默认名称" /></Form.Item>
        <Feedback error={actions.error} />
        <div><Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale || !eligible.length}>提交重跑实验</Button></div>
      </div>
    </Form>
  </Modal>;
}
