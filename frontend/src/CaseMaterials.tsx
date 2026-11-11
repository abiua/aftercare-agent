import { useEffect, useState } from 'react';
import { Alert, Button, Form, Input, Modal, Select, Space, Spin, Table } from 'antd';
import { EyeOutlined, FileImageOutlined } from '@ant-design/icons';
import { errorMessage, segment, type Client } from './api';
import type { CaseDetail, Material, User } from './contracts';
import { useActions } from './hooks';
import { Feedback, label, Placeholder, Status, time } from './ui';

type Props = {api: Client; detail: CaseDetail; user: User; base: string; refresh: () => void; canOperate: boolean};
type Editor = {kind: 'upload' | 'review'; version: number; materialsVersion: number};

export function CaseMaterials({api, detail, user, base, refresh, canOperate}: Props) {
  const [editor, setEditor] = useState<Editor>();
  const [preview, setPreview] = useState<Material>();
  const [notice, setNotice] = useState('');
  const canUpload = user.role === 'support' || user.role === 'customer';
  const latest = detail.reviews.find((item) => item.materials_version === detail.case.materials_version);
  const missing = detail.eligibility.missing_materials ?? [];
  const showPreview = preview && detail.materials.some((item) => item.id === preview.id);
  const editable = !['RESOLVED', 'EXECUTION_PENDING', 'EXECUTION_FAILED'].includes(detail.case.status);
  return <div className="stack">
    <div className="section-row">
      <div><h2>证明材料</h2><p className="muted">提交后由客服核对。当前审核状态 {latest ? <Status value={latest.decision} /> : '待审核'}</p></div>
      <Space wrap>
        {canUpload && <Button type="primary" icon={<FileImageOutlined />} disabled={!canOperate || !editable} onClick={() => setEditor({kind: 'upload', version: detail.case.version, materialsVersion: detail.case.materials_version})}>上传照片</Button>}
        {user.role === 'support' && <Button disabled={!canOperate || !editable || !detail.materials.length} onClick={() => setEditor({kind: 'review', version: detail.case.version, materialsVersion: detail.case.materials_version})}>记录人工审核</Button>}
      </Space>
    </div>
    {!!missing.length && <Alert type="warning" showIcon title="请补充证明照片" description={missing.map(label).join('、')} />}
    {!missing.length && latest?.decision === 'NEEDS_MORE' && <Alert type="warning" showIcon title="本次材料审核要求补充"
      description={latest.notes?.trim() || '请查看下方本次材料审核意见。'} />}
    <Feedback notice={notice} />
    <Table rowKey="id" dataSource={detail.materials} pagination={false} size="small" scroll={{x: 430}}
      locale={{emptyText: <Placeholder text="尚未收到照片" />}} columns={[
        {title: '材料', render: (_, item) => label(item.kind)},
        {title: '文件', render: (_, item) => item.filename ?? '已上传照片'},
        {title: '提交时间', render: (_, item) => time(item.created_at)},
        {title: '查看', render: (_, item) => <Button type="link" className="cell-link" icon={<EyeOutlined />} onClick={() => setPreview(item)}>查看照片</Button>},
      ]} />
    <h3>人工审核记录</h3>
    <Table rowKey="id" dataSource={detail.reviews} pagination={false} size="small" scroll={{x: 470}}
      locale={{emptyText: <Placeholder text="暂无人工审核意见" />}} columns={[
        {title: '结论', render: (_, item) => <Status value={item.decision} />},
        {title: '意见', render: (_, item) => item.notes || '—'},
        {title: '记录时间', render: (_, item) => time(item.created_at)},
        {title: '适用材料', render: (_, item) => item.materials_version === detail.case.materials_version ? '当前版本' : '历史版本'},
      ]} />
    {showPreview && <PhotoModal api={api} path={`${base}/materials/${segment(preview.id)}/content`} material={preview} close={() => setPreview(undefined)} />}
    {editor && <MaterialEditor api={api} base={base} editor={editor} currentVersion={detail.case.version} refresh={refresh}
      close={() => setEditor(undefined)} done={(message) => {setEditor(undefined); setNotice(message);}} />}
  </div>;
}

function PhotoModal({api, path, material, close}: {api: Client; path: string; material: Material; close: () => void}) {
  const [url, setUrl] = useState('');
  const [error, setError] = useState('');
  useEffect(() => {
    const controller = new AbortController();
    let objectUrl: string | undefined;
    api.blob(path, controller.signal).then((blob) => {
      if (controller.signal.aborted) return;
      if (!['image/jpeg', 'image/png', 'image/webp'].includes(blob.type)) {
        setError('无法验证图片格式，请联系售后处理。');
        return;
      }
      objectUrl = URL.createObjectURL(blob);
      setUrl(objectUrl);
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    });
    return () => {
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [api, path]);
  return <Modal title={label(material.kind)} open footer={null} onCancel={close} width={640}>
    <Feedback error={error} />
    {url && !error && <img className="material-preview" alt={material.filename || label(material.kind)} src={url} onError={() => setError('图片无法显示，请重新核对材料。')} />}
    {!url && !error && <div className="center"><Spin description="读取照片" /></div>}
  </Modal>;
}

function MaterialEditor({api, base, editor, currentVersion, refresh, close, done}: {
  api: Client; base: string; editor: Editor; currentVersion: number; refresh: () => void; close: () => void; done: (message: string) => void;
}) {
  const [file, setFile] = useState<File>();
  const [fileKey, setFileKey] = useState('');
  const [fileError, setFileError] = useState('');
  const actions = useActions(api);
  const upload = editor.kind === 'upload';
  const stale = currentVersion !== editor.version;
  function selectFile(value: File | undefined) {
    if (value && value.size > 8 * 1024 * 1024) {
      setFile(undefined); setFileError('照片超过 8 MB，请压缩后重新选择。'); return;
    }
    setFile(value); setFileError(''); setFileKey(crypto.randomUUID());
  }
  return <Modal title={upload ? '上传证明照片' : '记录人工审核'} open footer={null} onCancel={actions.busy ? undefined : close}>
    <Form layout="vertical" initialValues={{kind: 'ITEM'}} onFinish={async (values: {kind?: string; decision?: string; notes?: string}) => {
      if (stale || (upload && !file)) return;
      const message = upload ? '照片已提交，等待人工审核。' : '已记录本次人工审核。';
      if (upload && file) {
        const body = new FormData();
        body.set('kind', values.kind ?? 'ITEM');
        body.set('file', file);
        body.set('expected_version', String(editor.version));
        const result = await actions.submit(`${base}/materials`, body, {success: message, fingerprint: `${fileKey}:${values.kind}:${editor.version}`, refresh});
        if (result) done(message);
      } else {
        const result = await actions.submit(`${base}/material-reviews`, {expected_version: editor.version, decision: values.decision, notes: values.notes?.trim() ?? ''}, {success: message, refresh});
        if (result) done(message);
      }
    }}>
      {upload ? <>
        <Form.Item label="照片用途" name="kind" rules={[{required: true}]}>
          <Select options={['ITEM', 'PARCEL', 'WRONG_ITEM', 'LABEL'].map((value) => ({value, label: label(value)}))} />
        </Form.Item>
        <Form.Item label="选择照片" htmlFor="material-file" required extra="支持 JPG、PNG、WebP，最大 8 MB。请避免上传含无关个人信息的照片。">
          <input id="material-file" type="file" accept="image/jpeg,image/png,image/webp" onChange={(event) => selectFile(event.currentTarget.files?.[0])} />
        </Form.Item>
      </> : <>
        <p>本次意见将关联材料版本 {editor.materialsVersion}。</p>
        <Form.Item label="审核结论" name="decision" rules={[{required: true, message: '请选择审核结论'}]}>
          <Select options={['APPROVED', 'REJECTED', 'NEEDS_MORE'].map((value) => ({value, label: label(value)}))} />
        </Form.Item>
        <Form.Item label="审核意见" name="notes" rules={[{required: true, whitespace: true, message: '请填写审核理由或还需提供的材料'}]}><Input.TextArea rows={3} maxLength={1000} /></Form.Item>
      </>}
      <Space orientation="vertical" className="full">
        {stale && <Alert type="warning" showIcon title="工单状态已更新。请关闭后查看当前材料，再重新提交。" />}
        <Feedback error={fileError || actions.error} />
        <Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale || (upload && !file)}>{upload ? '提交照片' : '保存审核结论'}</Button>
      </Space>
    </Form>
  </Modal>;
}
