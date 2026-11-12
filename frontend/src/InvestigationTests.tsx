import {useState} from 'react';
import {Alert, Button, Card, Input, Select, Space, Table, Tag} from 'antd';
import {segment, type Client} from './api';
import {useResource} from './hooks';
import {Evidence, Feedback, Json, Reload, Status, time} from './ui';
import {decisionActions, isExecutionOption, type DecisionOption, type ProgramCheck} from './chatDecisionData';
import {decisionCheck} from './decisionLanguage';
import {investigationReason} from './investigationMessages';

type Usage = {calls:number; known_usd:string; unpriced_calls:number; reserved_usd:string; reserve_complete:boolean; estimated_usd:string|null; failed_calls:number};
type Run = Usage & {id:string;case_id:string;order_id:string;status:string;reason:string|null;created_at:string;duration_seconds:number|null;snapshot_version:number|null;mode:string;strategy:string};
type Detail = Usage & {id:string;case_id:string;status:string;reason:string|null;snapshots:{id:string;issue_id:string;version:number;sha256:string;input_through_seq:number;candidate:{options:DecisionOption[];unresolved:string[]};checks:Record<string,ProgramCheck>}[];attempts:{step:string;attempt:number;status:string;error:string|null;seconds:number|null;estimated_usd:string|null}[]};
const cost = (row: Usage) => row.estimated_usd !== null ? `$${row.estimated_usd}` : `已知 $${row.known_usd}，${row.unpriced_calls} 次费用待核对`;

export function InvestigationTests({api}:{api:Client}) {
  const [order,setOrder]=useState('');
  const [offset,setOffset]=useState(0);
  const [selected,setSelected]=useState<Run>();
  const [comparison,setComparison]=useState<string>();
  const [onlyProblems,setOnlyProblems]=useState(false);
  const list=useResource<{items:Run[];next_offset:number|null}>(api,`/evaluation/investigations?order=${segment(order)}&offset=${offset}`);
  const detail=useResource<Detail>(api,selected?`/evaluation/investigations/${segment(selected.id)}`:null);
  const other=useResource<Detail>(api,comparison?`/evaluation/investigations/${segment(comparison)}`:null);
  const rows=list.value?.items??[];
  const shown=onlyProblems?rows.filter(r=>r.failed_calls>0||r.unpriced_calls>0||!r.snapshot_version):rows;
  const current=detail.value?.id===selected?.id?detail.value:undefined;
  const prior=other.value?.id===comparison?other.value:undefined;
  const sameIssue=current?.snapshots[0]?.issue_id && current.snapshots[0].issue_id===prior?.snapshots[0]?.issue_id;
  function download() {
    if(!current)return;
    const url=URL.createObjectURL(new Blob([JSON.stringify({current,comparison:prior},null,2)],{type:'application/json'}));
    const a=document.createElement('a');a.href=url;a.download=`工单测试-${current.id}.json`;a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);
  }
  return <div className="stack">
    <div><h2>工单测试记录</h2><p>查看实际调查结果，对比修改提示词前后的建议。运行结束和方案正确是两回事，请结合原文与检查结果判断。</p></div>
    <Space wrap>
      <Input.Search aria-label="按订单查找测试" placeholder="订单号，例如 O9961" allowClear enterButton="查找" onSearch={value=>{setOrder(value.trim());setOffset(0);setSelected(undefined);setComparison(undefined);}} />
      <Button onClick={()=>setOnlyProblems(!onlyProblems)}>{onlyProblems?'显示全部记录':'只看调用失败或没有方案'}</Button>
      <Reload refresh={list.refresh} loading={list.loading}/>
    </Space>
    <Feedback error={list.error}/>
    <Table rowKey="id" dataSource={shown} loading={list.loading} pagination={false} scroll={{x:1000}} columns={[
      {title:'订单 / 版本',render:(_,r)=><Button type="link" onClick={()=>{setSelected(r);setComparison(undefined);}}>{r.order_id} · {r.snapshot_version?`第${r.snapshot_version}版`:'未生成方案'}</Button>},
      {title:'运行状态',render:(_,r)=><Status value={r.status}/>},
      {title:'模型调用',render:(_,r)=>`${r.calls} 次${r.failed_calls?`，${r.failed_calls} 次失败`:''}`},
      {title:'估算费用（美元）',render:(_,r)=>cost(r)},
      {title:'运行耗时',render:(_,r)=>r.duration_seconds===null?'进行中':`${Math.round(r.duration_seconds)} 秒`},
      {title:'开始时间',render:(_,r)=>time(r.created_at)},
    ]}/>
    <Space><Button disabled={!offset} onClick={()=>setOffset(Math.max(0,offset-30))}>上一页</Button><span>第 {Math.floor(offset/30)+1} 页</span><Button disabled={list.value?.next_offset==null} onClick={()=>setOffset(list.value!.next_offset!)}>下一页</Button></Space>
    {selected&&<Card title={`${selected.order_id} · 查看与对比`} extra={<Button onClick={()=>{setSelected(undefined);setComparison(undefined);}}>关闭</Button>}>
      <Space wrap><Button href={`?case=${selected.case_id}`}>查看工单</Button><Button disabled={!current} onClick={download}>导出本次对比</Button>
        <Select aria-label="选择对比版本" style={{minWidth:260}} placeholder="选择同工单的另一版" allowClear value={comparison} onChange={setComparison}
          options={rows.filter(r=>r.case_id===selected.case_id&&r.id!==selected.id).map(r=>({value:r.id,label:`${r.snapshot_version?`第${r.snapshot_version}版`:'无方案'} · ${time(r.created_at)}`}))}/>
      </Space>
      <p className="muted">对比选项来自当前页。同一工单的消息可能已有补充，不能仅凭措辞变化认定模型更好。</p>
      <Feedback error={detail.error||other.error}/>
      {prior&&!sameIssue&&<Alert type="warning" title="两次记录没有相同的售后事项快照，请分别查看。"/>}
      <div className={prior?'compare-grid':'stack'}>{[current,prior].filter((d):d is Detail=>!!d).map(d=><Result key={d.id} detail={d}/>)}</div>
    </Card>}
  </div>;
}

function Result({detail:d}:{detail:Detail}) {
  return <Card size="small" title={d.snapshots[0]?`第 ${d.snapshots[0].version} 版建议`:'没有生成方案'}>
    <p>{d.calls}次调用 · {cost(d)}</p>
    {!!d.unpriced_calls&&<p>暂留费用 ${d.reserved_usd}{d.reserve_complete?'':'（部分调用没有预留记录）'}</p>}
    {!d.snapshots.length&&<Alert type="warning" title={investigationReason(d.reason)||'这次运行没有保存方案，请查看调用记录。'}/>}
    {d.snapshots.map(s=><section key={s.id}>
      <p className="muted">依据截至第 {s.input_through_seq} 条消息的资料</p>
      {s.candidate.options.map(o=><section key={o.id}><h3>{decisionActions[o.action]} {isExecutionOption(o)&&o.action==='REFUND'?`¥${(o.lines.reduce((v,l)=>v+(l.amount_cents??0),0)/100).toFixed(2)}`:''}</h3><p>{o.rationale}</p><Tag>{decisionCheck(s.checks[o.id])}</Tag>{!isExecutionOption(o)&&<ul>{[...o.missing_evidence,...o.alternatives].map((v,i)=><li key={i}>{v}</li>)}</ul>}</section>)}
      {!!s.candidate.unresolved.length&&<Evidence title="尚未解决的问题"><ul>{s.candidate.unresolved.map((v,i)=><li key={i}>{investigationReason(v)||v}</li>)}</ul></Evidence>}
      <Evidence title="原始建议、引用与版本"><Json value={s}/></Evidence>
    </section>)}
    <Evidence title={`调用记录（${d.attempts.length}次）`}><Table rowKey={r=>`${r.step}:${r.attempt}`} dataSource={d.attempts} pagination={false} scroll={{x:500}} columns={[
      {title:'调用步骤',dataIndex:'step'},{title:'状态',render:(_,r)=><Status value={r.status}/>},
      {title:'耗时',render:(_,r)=>r.seconds===null?'未结束':`${r.seconds.toFixed(1)}秒`},
      {title:'错误',render:(_,r)=>investigationReason(r.error)||r.error||'—'},
      {title:'估算美元',render:(_,r)=>r.estimated_usd??'待核对'},
    ]}/></Evidence>
  </Card>;
}
