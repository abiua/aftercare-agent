import { useEffect, useMemo, useState } from 'react';
import { Alert, Button, Card, Space, Tag } from 'antd';
import { ApartmentOutlined, ExperimentOutlined, LogoutOutlined, SafetyCertificateOutlined } from '@ant-design/icons';
import { client, errorMessage } from './api';
import type { Runtime, User } from './contracts';
import { useResource } from './hooks';
import type { ChatProposalHandoff } from './chatDecisionData';
import { CustomerChat } from './CustomerChat';
import { Workbench } from './Workbench';
import { Evaluation } from './Evaluation';
import { Evidence, Feedback, Json, roles } from './ui';

const storageKey = 'aftercare-session-v1';
const readToken = () => sessionStorage.getItem(storageKey) ?? '';

export function App() {
  const [token, setToken] = useState(readToken);
  const [user, setUser] = useState<User>();
  const [error, setError] = useState('');
  const [verifying, setVerifying] = useState(false);
  const api = useMemo(() => client(token), [token]);

  useEffect(() => {
    if (!token) return;
    const controller = new AbortController();
    setVerifying(true);
    api.get<User>('/me', controller.signal).then((value) => {
      if (!controller.signal.aborted) setUser(value);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) {
        sessionStorage.removeItem(storageKey);
        setUser(undefined);
        setToken('');
        setError(errorMessage(failure));
        setVerifying(false);
      }
    }).finally(() => {
      if (!controller.signal.aborted) setVerifying(false);
    });
    return () => controller.abort();
  }, [token, api]);

  async function login(role: User['role']) {
    setError('');
    setVerifying(true);
    try {
      const result = await client('').post<{token: string}>('/auth/demo-role', {role});
      sessionStorage.setItem(storageKey, result.token);
      setToken(result.token);
    } catch (failure) {
      setError(errorMessage(failure));
      setVerifying(false);
    }
  }

  function logout() {
    sessionStorage.removeItem(storageKey);
    const url = new URL(window.location.href);
    url.searchParams.delete('case');
    url.searchParams.delete('conversation');
    history.replaceState(null, '', url);
    setToken('');
    setUser(undefined);
    setError('');
  }

  if (!user) return (
    <main className="login-shell">
      <div className="login-content">
        <Brand />
        <div className="eyebrow">客户服务 · 协作工作台</div>
        <h1>跟进每一笔售后。</h1>
        <p>在一处查看订单、补充材料与处理进度。客服和审批人员可根据各自权限办理工单。</p>
      </div>
      <Card className="login-card">
        <SafetyCertificateOutlined className="login-icon" />
        <h2>登录工作台</h2>
        <p className="muted">选择一个角色，直接进入演示。</p>
        <Space orientation="vertical" size={16} className="full">
          <Feedback error={error} />
          {([
            ['customer', '客户', '发起售后、补充照片、查看进度'],
            ['support', '客服', '调查工单、审核方案、回复客户'],
            ['approver', '财务审批人', '独立审批退款与换发'],
            ['tester', '测试员', '查看评测与演示结果'],
          ] as const).map(([role, label, description]) => <div key={role}>
            <Button block type={role === 'customer' ? 'primary' : 'default'} size="large"
              disabled={verifying} onClick={() => void login(role)}>以{label}身份进入</Button>
            <p className="muted" style={{margin: '6px 0 0'}}>{description}</p>
          </div>)}
        </Space>
      </Card>
    </main>
  );

  return <Workspace key={user.user_id} api={api} user={user} logout={logout} />;
}

function Brand() {
  return <div className="brand"><span className="brand-icon"><ApartmentOutlined /></span><span>售后协作台</span></div>;
}

function Workspace({api, user, logout}: {api: ReturnType<typeof client>; user: User; logout: () => void}) {
  const canChat = user.role === 'customer' || user.role === 'support';
  const [view, setView] = useState(() => {
    const query = new URLSearchParams(location.search);
    if (query.has('case')) return 'workbench';
    if (canChat && (user.role === 'customer' || query.has('conversation'))) return 'chat';
    return user.role === 'tester' ? 'evaluation' : 'workbench';
  });
  const [handoff, setHandoff] = useState<ChatProposalHandoff>();
  function openChat() {
    setHandoff(undefined);
    const url = new URL(location.href);
    url.searchParams.delete('case');
    history.replaceState(null, '', url);
    setView('chat');
  }
  function openCase(id: string, proposal?: ChatProposalHandoff) {
    setHandoff(proposal);
    const url = new URL(location.href);
    url.searchParams.set('case', id);
    history.replaceState(null, '', url);
    setView('workbench');
  }
  const runtime = useResource<Runtime>(api, user.role === 'customer' ? null : '/runtime');
  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="topbar-inner">
          <Brand />
          <nav aria-label="主导航">
            {canChat && <Button type="text" className={view === 'chat' ? 'nav-active' : ''} onClick={openChat}>{user.role === 'customer' ? '售后聊天' : '客户对话'}</Button>}
            <Button type="text" className={view === 'workbench' ? 'nav-active' : ''} onClick={() => setView('workbench')}>订单与工单</Button>
            {user.role === 'tester' && <Button type="text" icon={<ExperimentOutlined />} className={view === 'evaluation' ? 'nav-active' : ''} onClick={() => setView('evaluation')}>测试与评测</Button>}
          </nav>
          <Space size={10} className="account" wrap>
            <span>{user.display_name}</span><Tag>{roles[user.role]}</Tag>
            <Button type="text" icon={<LogoutOutlined />} onClick={logout}>退出</Button>
          </Space>
        </div>
      </header>
      <main className="page">
        {user.role !== 'customer' && runtime.value?.mode === 'offline' && <Alert className="runtime-banner" type="info" showIcon title="当前环境使用离线策略，款项与库存操作均为模拟。" />}
        {user.role !== 'customer' && runtime.value?.mode === 'live' && <Alert className="runtime-banner" type="warning" showIcon title="当前环境可调用真实模型。款项与库存操作均为模拟。" />}
        {view === 'chat' ? <CustomerChat api={api} user={user} onCase={openCase} /> : view === 'workbench' ? <Workbench api={api} user={user} runtime={runtime.value} handoff={handoff} clearHandoff={() => setHandoff(undefined)} /> : <Evaluation api={api} runtime={runtime.value} />}
        <footer className="footer"><span>售后协作台 · 操作记录可追溯</span>{user.role !== 'customer' && runtime.value && <Evidence title="运行环境"><Json value={runtime.value} /></Evidence>}</footer>
      </main>
    </div>
  );
}
