import { Alert, Button, Card, Select } from 'antd';
import { useEffect, useState } from 'react';
import type { Client } from './api';
import { useResource } from './hooks';

type Batch = {id:string; label:string; start:number; end:number|null; state:string};
type Catalog = {items:Batch[]; undated:string[]};
type Point = [number,number];
type Timeline = {first?:number; start:number; end:number; step:number; batch:Batch|null; resource_scope:string; series:Record<string,Point[]>};
type Measure = {key:string; label:string; unit:string; scale?:number};
const panels: {title:string; color:string; measures:Measure[]}[] = [
  {title:'资源占用',color:'#25756a',measures:[
    {key:'cpu_percent',label:'系统 CPU',unit:'%'},{key:'memory_percent',label:'系统内存',unit:'%'},
    {key:'api_rss_bytes',label:'API 进程内存',unit:'MiB',scale:1048576},{key:'disk_free_bytes',label:'磁盘剩余',unit:'GiB',scale:1073741824},
    {key:'network_bytes_per_second',label:'网络收发',unit:'KiB/s',scale:1024},{key:'redis_memory_bytes',label:'Redis 内存',unit:'MiB',scale:1048576}]},
  {title:'API 性能',color:'#3468a2',measures:[{key:'api_p95_ms',label:'请求 P95',unit:'ms'},{key:'requests_per_second',label:'请求速率',unit:'次/秒'},{key:'error_percent',label:'5xx 错误率',unit:'%'}]},
  {title:'队列积压',color:'#a4671c',measures:[{key:'queue_messages',label:'Redis 待取消息',unit:'条'},
    {key:'waiting_jobs',label:'业务待处理任务',unit:'个'},{key:'oldest_wait_seconds',label:'最老任务等待',unit:'秒'}]},
  {title:'模型费用',color:'#845997',measures:[{key:'benchmark_known_usd',label:'自选评测累计',unit:'USD'},
    {key:'service_known_usd',label:'客服累计',unit:'USD'},{key:'benchmark_unpriced_calls',label:'评测费用待核清',unit:'次'},
    {key:'service_unpriced_calls',label:'客服费用待核清',unit:'次'}]},
];
const stamp = (n:number) => new Date(n*1000).toLocaleString('zh-CN',{hour12:false});
const short = (n:number) => new Date(n*1000).toLocaleTimeString('zh-CN',{hour12:false});
const display = (value:number, m:Measure) => (value/(m.scale ?? 1)).toLocaleString('zh-CN',
  {maximumFractionDigits:m.unit === 'USD' ? 6 : 2}) + ' ' + m.unit;

function Track({panel, data, time}: {panel:typeof panels[number]; data:Timeline; time:number}) {
  const [key,setKey] = useState(panel.measures[0].key);
  const m = panel.measures.find(x=>x.key===key) ?? panel.measures[0];
  const points = data.series[key] ?? [];
  const at = points.find(([t])=>Math.abs(t-time)<0.002);
  const max = Math.max(...points.map(p=>p[1]), m.unit==='%'?100:0) || 1;
  const x = (t:number)=> 1000*(t-data.start)/Math.max(.001,data.end-data.start);
  const y = (v:number)=> 47-43*v/max;
  const path = points.map(([t,v],i)=> (i && t-points[i-1][0] <= data.step*1.01 ? 'L':'M') + x(t)+','+y(v)).join(' ');
  return <div className="ops-track">
    <div className="ops-track-head"><strong>{panel.title}</strong>
      <Select size="small" aria-label={panel.title+'指标'} value={key} onChange={setKey}
        options={panel.measures.map(i=>({value:i.key,label:i.label}))}/>
      <b style={{color:panel.color}}>{at ? display(at[1],m) : '无样本'}</b></div>
    {points.length ? <svg viewBox="0 0 1000 50" preserveAspectRatio="none" role="img" aria-label={m.label+'趋势'}>
      <path d="M0 47H1000 M0 25H1000" stroke="#e5e9ed" fill="none"/>
      <path d={path} stroke={panel.color} strokeWidth="2" fill="none" vectorEffect="non-scaling-stroke"/>
      {points.map(([t,v],i)=> (i===0 || t-points[i-1][0]>data.step*1.01) &&
        <circle key={t} cx={x(t)} cy={y(v)} r="2" fill={panel.color}/>)}
      <path d={'M'+x(time)+' 0V50'} stroke="#586875" strokeDasharray="3 3" vectorEffect="non-scaling-stroke"/>
    </svg> : <div className="ops-empty">{key==='api_p95_ms'?'此时段没有可计算 P95 的请求样本':'此时段暂无采样'}</div>}
  </div>;
}

export function OperatingOverview({api}: {api:Client}) {
  const [batch,setBatch] = useState(()=>new URLSearchParams(location.search).get('batch') ?? '');
  const [minutes,setMinutes] = useState(15);
  const [cursor,setCursor] = useState<number>();
  const catalog = useResource<Catalog>(api,'/operations/batches');
  const timeline = useResource<Timeline>(api,'/operations/timeline?'+(batch?'batch='+encodeURIComponent(batch):'minutes='+minutes));
  const data = timeline.value;
  const {refresh:reloadData} = timeline;
  const {refresh:reloadBatches} = catalog;
  const ended = !!data?.batch?.end;
  useEffect(()=>{
    const timer = setInterval(()=>{if(!ended) reloadData(); reloadBatches();},10000);
    return ()=>clearInterval(timer);
  },[reloadData,reloadBatches,ended]);
  function chooseBatch(id:string) {
    setBatch(id); setCursor(undefined);
    const url = new URL(location.href);
    if (id) url.searchParams.set('batch',id); else url.searchParams.delete('batch');
    history.replaceState(null,'',url);
  }
  const first = data?.first ?? data?.start ?? 0;
  const last = data ? Math.floor((data.end-first)/data.step + 1e-7) : 0;
  const index = Math.min(last,cursor ?? last);
  const time = data ? first + index*data.step : 0;
  const scrape = data?.series.scrape_ok?.find(([t])=>Math.abs(t-time)<.002)?.[1];
  return <div className="stack">
    <section className="hero"><div><h1>系统运行总览</h1><p>对齐资源、请求、队列和费用，查看一次评测运行期间系统发生了什么。</p></div></section>
    <Card className="data-card"><div className="ops-controls">
      <label>评测批次<Select aria-label="评测批次" value={batch} onChange={chooseBatch} style={{minWidth:235}}
        options={[{value:'',label:'实时总览'},...(catalog.value?.items ?? []).map(j=>({
          value:j.id,label:stamp(j.start)+' · '+j.label+(j.state==='interrupted'?' · 结束时间未知':''),
          disabled:j.state==='interrupted'}))]}/></label>
      {!batch && <label>时间范围<Select aria-label="时间范围" value={minutes} onChange={v=>{setMinutes(v); setCursor(undefined);}}
        options={[{value:15,label:'最近 15 分钟'},{value:60,label:'最近 1 小时'},{value:360,label:'最近 6 小时'},{value:1440,label:'最近 24 小时'}]}/></label>}
      <Button onClick={()=>{reloadData();reloadBatches();}}>刷新</Button>
      {batch && <a href={'?eval=jobs&run_batch='+encodeURIComponent(batch)}>查看批次结果</a>}
    </div>
    {catalog.error && <Alert type="warning" title={catalog.error}/>}
    {!!catalog.value?.undated.length && <details><summary>缺少起止时间或采集环境的历史批次（{catalog.value.undated.length}）</summary>
      <p>{catalog.value.undated.join('、')}</p></details>}</Card>
    {timeline.error && <Alert type="warning" title={timeline.error}/>}
    {!timeline.error && !data && <Card loading/>}
    {data && !timeline.error && <Card className="data-card"><div className="ops-period"><h2>{batch?'批次运行时段':'系统时间轴'}</h2>
      <span>{stamp(data.start)} 至 {stamp(data.end)}{data.batch && !ended && ' · 运行中'}</span></div>
      <div className="ops-meta">{data.resource_scope} · 每 5 秒采样 · 接口 P95 使用最近 30 秒请求 · 费用按已记录用量估算，显示采样时的累计值</div>
      {scrape === 0 && <Alert type="warning" title="该时刻 Prometheus 未能采集 API 指标"/>}
      {panels.map(panel=><Track key={panel.title} panel={panel} data={data} time={time}/>)}
      <label className="ops-cursor">对照时刻 {stamp(time)}
        <input aria-label="对照时刻" type="range" min={0} max={last} value={index}
          onChange={e=>setCursor(Number(e.target.value))}/></label>
      <div className="ops-axis"><span>{short(data.start)}</span><span>{short(data.end)}</span></div>
    </Card>}
  </div>;
}
