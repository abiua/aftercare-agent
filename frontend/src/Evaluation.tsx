import { Alert, Card, Tabs } from 'antd';
import { useEffect, useState } from 'react';
import type { Client } from './api';
import { BadCases } from './BadCases';
import { BlindReview } from './BlindReview';
import type { EvaluationCapabilities, Runtime } from './contracts';
import { readEvaluationLocation, writeEvaluationLocation, type EvaluationLocation } from './evaluationRoute';
import { useResource } from './hooks';
import { InvestigationCampaigns } from './InvestigationCampaigns';
import { InvestigationTests } from './InvestigationTests';
import { Placeholder, Reload } from './ui';

export function Evaluation({api}: {api: Client; runtime?: Runtime}) {
  const [route, setRoute] = useState(() => readEvaluationLocation(window.location.search));
  const capabilities = useResource<EvaluationCapabilities>(api, '/evaluation/capabilities');
  const blindEnabled = !capabilities.error && capabilities.value?.chat_blind_review === true;
  const blindScope = route.tab === 'blind';
  const tab = ['bad-cases', 'investigations', 'blind'].includes(route.tab) ? route.tab : 'campaigns';

  useEffect(() => {
    const update = () => setRoute(readEvaluationLocation(window.location.search));
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);

  function navigate(key: string) {
    const next: EvaluationLocation = {tab: key as EvaluationLocation['tab']};
    const href = writeEvaluationLocation(window.location.href, next);
    if (href !== window.location.href) history.pushState(null, '', href);
    setRoute(next);
  }

  return <div className="stack">
    <section className="hero">
      <div><div className="eyebrow">测试与评测 / 质量工作台</div><h1>测试中台</h1><p>比较三种 Agent 在官方零售任务上的表现，查看工单调查和复核记录。</p></div>
    </section>
    <Card className="data-card">
      <Tabs activeKey={tab} onChange={navigate} items={[
        {key:'campaigns', label:'官方 τ²-Bench', children:tab === 'campaigns' && <InvestigationCampaigns api={api}/>},
        {key:'bad-cases', label:'问题与回归', children:tab === 'bad-cases' && <BadCases api={api}/>},
        {key:'investigations', label:'工单测试记录', children:tab === 'investigations' && <InvestigationTests api={api}/>},
        ...(blindEnabled || blindScope ? [{key:'blind', label:'独立盲审', children:blindScope && (blindEnabled ?
          <BlindReview api={api}/> : capabilities.error ?
            <Alert type="error" showIcon title="暂时无法检查盲审是否可用" description={capabilities.error}
              action={<Reload refresh={capabilities.refresh} loading={capabilities.loading}/>}/> :
            capabilities.value ? <Alert type="info" showIcon title="当前环境未开放独立盲审"/> :
              <Placeholder text="正在检查独立盲审是否可用"/>)}] : []),
      ]}/>
    </Card>
  </div>;
}
