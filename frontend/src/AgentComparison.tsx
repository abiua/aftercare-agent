import {useRef, useState} from 'react';
import {Alert, Button, Form, Input, InputNumber, Modal, Select, Table} from 'antd';
import type {Client} from './api';
import type {Dataset, EvalCase, Experiment, Items, Runtime} from './contracts';
import {useActions, useResource} from './hooks';
import {Feedback} from './ui';

export function EvaluationMetricsGuide() {
  const rows=[
    ['业务结果','动作、退款金额、补证和转人工是否正确','程序检查与独立人工判断分开；无审核标签显示待评'],
    ['证据与政策','遗漏事实、错用条款、混用材料','逐项记录来源和审核结论'],
    ['安全与纠错','错误建议是否被拦住、补查是否纠正错误','保留错误与修正前后的证据'],
    ['性能与费用','调用次数、耗时、费用、超时与失败','实际测量；未知费用单列'],
    ['对比收益','改善几案、退步几案，多花多少时间和钱','相同案例、模型、工具与预算；普通/复杂场景分开'],
  ];
  return <Table size="small" pagination={false} rowKey={r=>r[0]} dataSource={rows} columns={[
    {title:'指标组',render:(_,r)=>r[0]}, {title:'记录什么',render:(_,r)=>r[1]}, {title:'怎样判断',render:(_,r)=>r[2]},
  ]}/>;
}

type Values={name:string;cases:string[];source:'fixture'|'live';strategy:'economy'|'analysis';repetitions:number;max_calls:number;budget_usd:number};
export function AgentComparison({api,runtime,dataset,close,created}:{api:Client;runtime?:Runtime;dataset:Dataset;close:()=>void;created:(left:string,right:string)=>void}) {
  const cases=useResource<Items<EvalCase>>(api,`/evaluation/cases?dataset=${encodeURIComponent(dataset.id)}`);
  const actions=useActions(api);
  const [left,setLeft]=useState<string>();
  const [frozen,setFrozen]=useState<Values>();
  const [submitting,setSubmitting]=useState(false);
  const active=useRef(false);
  const [form]=Form.useForm<Values>();
  const source=Form.useWatch('source',form);
  async function run(values:Values) {
    if(active.current)return;
    active.current=true;setSubmitting(true);
    const v=frozen??values;setFrozen(v);
    try {
      const config={dataset_id:dataset.id,source:v.source,strategy:v.strategy,repetitions:v.repetitions,concurrency:1,max_calls:v.max_calls,budget_usd:v.source==='live'?v.budget_usd:0,case_ids:v.cases};
      let first=left;
      if(!first){
        const result=await actions.submit<Experiment>('/evaluation/experiments',{...config,mode:'single',name:`${v.name} · 单Agent`},{success:''});
        if(!result)return;
        first=result.id;setLeft(first);
      }
      const second=await actions.submit<Experiment>('/evaluation/experiments',{...config,mode:'multi',name:`${v.name} · 多Agent`},{success:''});
      if(second)created(first,second.id);
    } finally {active.current=false;setSubmitting(false);}
  }
  return <Modal open title="运行单 / 多 Agent 对比" width={720} footer={null} onCancel={submitting?undefined:close}>
    <Alert type="info" title="相同条件，分别运行单Agent和多Agent" description="此入口使用已有售后评测流程（单Agent / 串行多角色）。新版聊天强单Agent、三阶段和并行多Agent尚未接入，不能用这里的成绩证明新版协作收益。"/>
    <Form form={form} layout="vertical" initialValues={{name:'单多Agent对比',source:'fixture',strategy:'analysis',repetitions:1,max_calls:24,budget_usd:0.5}} onFinish={run}>
      <Form.Item label="测试名称" name="name" rules={[{required:true,whitespace:true,max:100}]}><Input disabled={!!frozen}/></Form.Item>
      <Form.Item label="选择相同案例" name="cases" rules={[{required:true,type:'array',min:1}]}><Select disabled={!!frozen} mode="multiple" loading={cases.loading} options={(cases.value?.items??[]).map(c=>({value:c.id,label:`${c.id} · ${c.scenario} · ${c.difficulty}`}))}/></Form.Item>
      <div className="form-grid">
        <Form.Item label="运行方式" name="source"><Select disabled={!!frozen} options={[{value:'fixture',label:'离线验证操作流程'},{value:'live',label:'真实模型评测',disabled:runtime?.mode!=='live'}]}/></Form.Item>
        <Form.Item label="两组使用同一个模型" name="strategy"><Select disabled={!!frozen} options={[{value:'analysis',label:'分析模型'},{value:'economy',label:'轻量模型'}]}/></Form.Item>
        <Form.Item label="每案重复次数" name="repetitions"><InputNumber disabled={!!frozen} min={1} max={3} precision={0}/></Form.Item>
        <Form.Item label="每组调用总上限" name="max_calls"><InputNumber disabled={!!frozen} min={1} max={144} precision={0}/></Form.Item>
        {source==='live'&&<Form.Item label="每组费用上限（美元，两组各一份）" name="budget_usd" rules={[{required:true,type:'number',min:0.0001,max:10}]}><InputNumber disabled={!!frozen} min={0.0001} max={10}/></Form.Item>}
      </div>
      {source==='fixture'&&<p>不调用模型、不产生模型费用；离线结果不能证明多Agent更强。</p>}
      {left&&<Alert type="info" title="单Agent实验已创建；重试只继续提交多Agent组。"/>}
      <Feedback error={actions.error||cases.error}/>
      <Button type="primary" htmlType="submit" loading={submitting} disabled={!!cases.error||cases.loading}>{left?'继续提交多Agent组':'启动两组测试'}</Button>
    </Form>
  </Modal>;
}
