import { useState } from 'react';
import { Alert, Button, Card, Checkbox, Descriptions, Form, Input, Radio, Select, Space, Table, Tag } from 'antd';
import { segment, type Client } from './api';
import type { EvalResult, EvalReview, ReviewDecision } from './contracts';
import { useActions } from './hooks';
import { buildReviewAnnotations, recordedAnnotations, reviewDecisionLabels, reviewGroups, reviewItemLimit, reviewSections, type ReviewChoices } from './reviewAnnotations';
import { Evidence, Feedback, Json, time } from './ui';

const reviewable = new Set(['PASSED', 'FAILED', 'TIMEOUT', 'ERROR', 'NEEDS_REVIEW']);
const overallLabels = {PASS: '通过', FAIL: '不通过', UNKNOWN: '无法判断'};
const decisions = Object.keys(reviewDecisionLabels) as ReviewDecision[];
type ReviewForm = {
  decision: ReviewDecision;
  notes: string;
  replaceConfirmed?: boolean;
  items?: ReviewChoices;
};

function AnnotationHistory({review, sections}: {review: EvalReview; sections: ReturnType<typeof reviewSections>}) {
  const groups = sections.flatMap(({group, title, items}) => {
    const entries = recordedAnnotations(review.annotations, group);
    if (!entries.length) return [];
    const titles = new Map(items.map((item) => [item.key, item.title]));
    return [{group, title, entries, titles}];
  });
  if (!groups.length) return <p className="muted">未提交逐项标注</p>;
  return <Evidence title="查看该版逐项标注">
    {review.decision === 'UNKNOWN' && <p className="muted">该版总评为无法判断，统计未采用该版逐项标注。</p>}
    {groups.map(({group, title, entries, titles}) => <section key={group}>
      <h4>{title}</h4>
      <ul>{entries.map(([key, decision], index) => <li key={key}>
        {titles.get(key) ?? `第 ${index + 1} 项（当前报告缺少对应证据）`} <Tag>{reviewDecisionLabels[decision]}</Tag>
      </li>)}</ul>
    </section>)}
    <Evidence title="查看此版原始标注"><Json value={review.annotations} /></Evidence>
  </Evidence>;
}

export function EvaluationReview({api, result, refresh}: {api: Client; result: EvalResult; refresh: () => void}) {
  const actions = useActions(api);
  const [reviewVersion, setReviewVersion] = useState<number>();
  const [form] = Form.useForm<ReviewForm>();
  const overall = Form.useWatch('decision', form);
  const stale = reviewVersion !== result.version;
  const canReview = reviewable.has(result.machine_status ?? result.status);
  const sections = reviewSections(result);
  const latest = result.reviews.at(-1);
  const replacesLabels = reviewGroups.some((group) => recordedAnnotations(latest?.annotations, group).length > 0);
  const [limit, setLimit] = useState('');
  const closeEditor = () => {form.resetFields(); setLimit(''); setReviewVersion(undefined);};
  return <>
    <Card size="small" title="人工复核历史">
      <Table rowKey="id" dataSource={result.reviews} pagination={false} size="small" scroll={{x: 620}} columns={[
        {title: '版本', render: (_, row) => row.version ?? '—'},
        {title: '结论', render: (_, row) => Object.hasOwn(overallLabels, row.decision) ? overallLabels[row.decision as ReviewDecision] : '待核对'},
        {title: '复核人', dataIndex: 'reviewer_id'},
        {title: '意见', dataIndex: 'notes'},
        {title: '时间', render: (_, row) => time(row.created_at)},
        {title: '逐项判断', render: (_, row) => <AnnotationHistory review={row} sections={sections} />},
      ]} locale={{emptyText: '暂无人工复核'}} />
    </Card>
    <Feedback error={actions.error} notice={actions.notice} />
    {reviewVersion === undefined && canReview && <div>
      <Button type="primary" onClick={() => setReviewVersion(result.version)}>添加人工复核</Button>
    </div>}
    {reviewVersion !== undefined && <Form form={form} layout="vertical" onValuesChange={() => setLimit('')}
      disabled={actions.busy} onFinish={async ({decision, notes, items, replaceConfirmed}) => {
        if (stale || !canReview || (replacesLabels && !replaceConfirmed)) return;
        const parsed = buildReviewAnnotations(sections, items ?? {});
        if (parsed.overLimit) {
          const section = sections.find(({group}) => group === parsed.overLimit);
          setLimit(`${section?.title}最多可提交 ${reviewItemLimit} 项。请将多出的选项改为暂不评。`);
          return;
        }
        const posted = await actions.submit(`/evaluation/results/${segment(result.id)}/reviews`,
          {decision, notes: notes.trim(), expected_version: reviewVersion, ...(parsed.annotations && {annotations: parsed.annotations})},
          {success: '已提交复核意见，历史记录已保留。', refresh});
        if (posted) closeEditor();
      }}>
      <div className="stack">
        <h3>逐项复核（可选）</h3>
        <p className="muted">从证据出发独立判断。每次新复核都从「暂不评」开始，当前统计使用最新一版。这里评价本次回答。</p>
        {overall === 'UNKNOWN' && <Alert showIcon type="info" title="本次总评为无法判断，所选逐项标注只保留在历史中。" />}
        {sections.map(({group, title, question, items, empty}) => <Card key={group} size="small" title={title}>
          {items.length ? <div className="stack">
            <p>{question}</p>
            {items.map((item, index) => <Card key={item.key} size="small" title={item.title}>
              <Descriptions size="small" column={1} items={item.evidence.map(({label, text}, evidenceIndex) => ({
                key: evidenceIndex, label, children: text,
              }))} />
              <Evidence title="查看该项原始记录"><Json value={{key: item.key, ...item.raw}} /></Evidence>
              <Form.Item name={['items', group, index]} initialValue="UNSELECTED" noStyle>
                <Radio.Group aria-label={`${item.title}的判断`} options={[
                  {value: 'UNSELECTED', label: '暂不评'},
                  ...decisions.map((value) => ({value, label: reviewDecisionLabels[value]})),
                ]} />
              </Form.Item>
            </Card>)}
          </div> : <p className="muted">{empty}</p>}
        </Card>)}
        <Form.Item label="复核结论" name="decision" rules={[{required: true, message: '请选择复核结论'}]}>
          <Select options={decisions.map((value) => ({value, label: overallLabels[value]}))} />
        </Form.Item>
        <Form.Item label="证据与说明" name="notes" rules={[{required: true, whitespace: true, message: '请填写复核依据'}]}>
          <Input.TextArea rows={3} maxLength={1500} />
        </Form.Item>
        {replacesLabels && <Alert type="warning" showIcon title="当前统计只采用最新版本的逐项判断，旧版留在历史中。"
          description={<Form.Item name="replaceConfirmed" valuePropName="checked" rules={[{
            validator: (_, confirmed) => confirmed ? Promise.resolve() : Promise.reject(new Error('请确认新版本的替换范围')),
          }]} style={{marginBottom: 0}}><Checkbox>确认按本次选择保存</Checkbox></Form.Item>} />}
        {limit && <Alert type="warning" showIcon title={limit} />}
        {(stale || !canReview) && <Alert type="warning" showIcon title="案例结果或复核记录已更新，请取消编辑并核对最新记录。" />}
        <Space wrap><Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale || !canReview}>保存复核</Button>
          <Button onClick={closeEditor}>取消编辑</Button></Space>
      </div>
    </Form>}
  </>;
}
