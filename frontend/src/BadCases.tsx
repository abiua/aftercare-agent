import { Alert, Descriptions, Select, Table, Tag } from 'antd';
import { useState } from 'react';
import type { Client } from './api';
import { useResource } from './hooks';
import { Feedback, Reload } from './ui';

type Entry = {id:string; category:string; phase:string; title:string; root_cause:string; status:string; fix:string; input_note:string; regression:{status:string;report:string}; evidence:{kind:string;path:string;sha256:string;verification:string}[]};
const categories: Record<string,string> = {parsing:'文档解析',chunking:'文档切片',retrieval:'检索召回',ranking:'排序',policy:'政策适用',reasoning:'判断与推理',tools:'工具调用',runtime:'超时与恢复',memory:'会话记忆'};
const statuses: Record<string,string> = {OPEN:'待解决',PARTIAL:'部分改善',FIXED:'已修复',REGRESSION_COVERED:'已有回归用例'};
export function BadCases({api}:{api:Client}) {
  const resource = useResource<{items:Entry[]}>(api,'/evaluation/bad-cases');
  const [category,setCategory] = useState<string>();
  return <div className="stack">
    <Alert type="info" showIcon title="问题记录与修复跟踪" description="保留原报告和未解决的问题。工程回归结果与客服独立审核分开记录；已有回归用例不代表发生过该错误。" />
    <div className="section-row"><Select allowClear placeholder="全部环节" style={{width:180}} value={category} onChange={setCategory} options={Object.entries(categories).map(([value,label])=>({value,label}))}/><Reload refresh={resource.refresh} loading={resource.loading}/></div>
    <Feedback error={resource.error}/>
    <Table<Entry> rowKey="id" loading={resource.loading} dataSource={(resource.value?.items??[]).filter(x=>!category||x.category===category)} columns={[
      {title:'问题',dataIndex:'title'},{title:'环节',render:(_,r)=>categories[r.category]??r.category},{title:'状态',render:(_,r)=><Tag>{statuses[r.status]??r.status}</Tag>},
      {title:'证据',render:(_,r)=>r.evidence.every(e=>e.verification==='MATCH')?'原件一致':'需要核对原件'},
    ]} expandable={{expandedRowRender:r=><Descriptions column={1} bordered size="small" items={[
      {key:'input',label:'输入与输出',children:r.input_note},{key:'root',label:'原因',children:r.root_cause},{key:'fix',label:'修复与剩余问题',children:r.fix},
      {key:'test',label:'回归报告',children:r.regression.report},
      {key:'refs',label:'原始记录',children:r.evidence.map(e=><div key={e.path}><code>{e.path}</code> · {e.verification==='MATCH'?'内容未变':e.verification==='MISSING'?'文件缺失':'内容已变，请复核'}</div>)},
    ]}/>}}/>
  </div>;
}
