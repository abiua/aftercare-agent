import { useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, Space } from 'antd';
import { ApiError, errorMessage, type Client } from './api';
import type { RefundLookupExecution, RefundLookupResponse, RefundLookupResult } from './contracts';
import { describeRefundExecution, matchingRefundExecution, refundExecutionId, refundLookupMessage } from './refundLookup';
import { Feedback, money, time } from './ui';

type LookupQuery = {id: string; base: string};
type LookupAnswer = {query: LookupQuery; execution?: RefundLookupExecution; error?: string};

export function useRefundLookup(api: Client, base: string | undefined) {
  const [input, setInput] = useState('');
  const [validation, setValidation] = useState('');
  const [selected, setSelected] = useState<LookupQuery>();
  const [answer, setAnswer] = useState<LookupAnswer>();
  const query = base && selected?.base === base ? selected : undefined;

  useEffect(() => {
    if (!query) return;
    const controller = new AbortController();
    const params = new URLSearchParams({execution_id: query.id});
    api.get<RefundLookupResponse>(`${query.base}/refund-lookup?${params}`, controller.signal).then((response) => {
      if (controller.signal.aborted) return;
      const execution = matchingRefundExecution(response, query.id);
      setAnswer(execution ? {query, execution} : {query, error: '服务返回的记录与查询编号不符，请重试。'});
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setAnswer({query, error: error instanceof ApiError && error.status === 404
        ? refundLookupMessage('NOT_FOUND') : errorMessage(error)});
    });
    return () => controller.abort();
  }, [api, query]);

  function edit(value: string) {
    setInput(value);
    setValidation('');
    setSelected(undefined);
  }

  function submit(value: string) {
    edit(value);
    if (!base) return;
    const id = refundExecutionId(value);
    if (!id) {
      setValidation(refundLookupMessage(value.trim() ? 'INVALID_REFERENCE' : 'REFERENCE_REQUIRED'));
      return;
    }
    setInput(id);
    setSelected({id, base});
  }

  const current = query && answer?.query === query ? answer : undefined;
  return {input, edit, submit, validation, loading: !!query && !current, current};
}

export function RefundExecutionDetails({execution}: {execution: RefundLookupExecution}) {
  const presentation = describeRefundExecution(execution);
  return <Descriptions size="small" column={{xs: 1, sm: 2}} items={[
    {key: 'id', label: '退款执行编号', children: <span style={{overflowWrap: 'anywhere'}}>{execution.id}</span>},
    {key: 'status', label: '模拟处理状态', children: presentation.status},
    {key: 'amount', label: '该笔登记金额', children: money(execution.amount_cents)},
    {key: 'created', label: '记录创建时间', children: time(execution.created_at)},
    {key: 'receipt', label: '模拟回执', children: presentation.receipt},
  ]} />;
}

export function RefundLookupOutcome({result}: {result: RefundLookupResult}) {
  return result.status === 'FOUND' ? <div>
    <RefundExecutionDetails execution={result.execution} />
    <p className="muted">这是本地模拟处理记录，实际到账需另行核对。</p>
  </div> : <p>{refundLookupMessage(result.status)}</p>;
}

export function RefundLookupPanel({lookup, disabled}: {lookup: ReturnType<typeof useRefundLookup>; disabled: boolean}) {
  const {input, edit, submit, validation, loading, current} = lookup;
  return <Card size="small" title="查询指定退款">
    <div className="stack">
      <p className="muted">可输入完整的退款执行编号，也可在订单记录中选择一笔。查询仅读取原记录。</p>
      <Form layout="vertical" onFinish={() => submit(input)}>
        <Form.Item label="退款执行编号" htmlFor="refund-execution-id" validateStatus={validation ? 'error' : undefined} help={validation || undefined}>
          <Input id="refund-execution-id" value={input} maxLength={160} disabled={disabled}
            onChange={(event) => edit(event.target.value)} />
        </Form.Item>
        <Space wrap>
          <Button type="primary" htmlType="submit" disabled={disabled} loading={loading}>查询退款执行记录</Button>
          <Button disabled={!input && !validation} onClick={() => edit('')}>清空查询</Button>
        </Space>
      </Form>
      <Feedback error={current?.error} />
      {current?.execution && <Alert type="info" showIcon title="找到指定退款执行记录"
        description={<RefundLookupOutcome result={{status: 'FOUND', execution: current.execution}} />} />}
    </div>
  </Card>;
}
