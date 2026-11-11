import { createRoot } from 'react-dom/client';
import { ConfigProvider } from 'antd';
import zhCN from 'antd/locale/zh_CN';
import { App } from './App';
import 'antd/dist/reset.css';
import './styles.css';

createRoot(document.getElementById('root')!).render(
  <ConfigProvider locale={zhCN} theme={{
    token: {colorPrimary: '#176b56', borderRadius: 10, fontFamily: "'PingFang SC','Microsoft YaHei',system-ui,sans-serif", colorText: '#20332e'},
    components: {Card: {headerFontSize: 16}, Table: {headerBg: '#eff5f1'}, Tabs: {titleFontSize: 15}},
  }}><App /></ConfigProvider>,
);
