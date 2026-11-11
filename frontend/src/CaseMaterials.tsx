import { useEffect, useState } from 'react';
import { Alert, Button, Collapse, Form, Input, Modal, Select, Space, Spin, Table } from 'antd';
import { EyeOutlined, FileImageOutlined } from '@ant-design/icons';
import { errorMessage, segment, type Client } from './api';
import type { CaseDetail, Material, MaterialFinding, MaterialObservationInput, User } from './contracts';
import { useActions } from './hooks';
import { Feedback, label, Placeholder, Status, time } from './ui';

type Props = {api: Client; detail: CaseDetail; user: User; base: string; refresh: () => void; canOperate: boolean};
type Editor = {kind: 'upload' | 'review'; version: number; materialsVersion: number};
type ObservationDraft = {finding?: MaterialFinding; line?: string; quantity?: string};
const findings: Record<MaterialFinding, string> = {
  VISIBLE_DAMAGE: '可见破损', NO_VISIBLE_DAMAGE: '未观察到可见破损', UNCERTAIN: '无法判断',
};
const findingOptions = Object.entries(findings).map(([value, label]) => ({value, label}));
const materialName = (item: Material) => item.filename ? `${label(item.kind)} · ${item.filename}` : label(item.kind);


export function CaseMaterials({api, detail, user, base, refresh, canOperate}: Props) {
  const [editor, setEditor] = useState<Editor>();
  const [preview, setPreview] = useState<Material>();
  const [notice, setNotice] = useState('');
  const canUpload = user.role === 'support' || user.role === 'customer';
  const scopedLines = detail.order.items.filter((item) => detail.case.line_ids.includes(item.line_id)).map((item) => ({
    id: item.line_id, quantity: item.quantity,
    name: `${detail.order.products.find((product) => product.id === item.product_id)?.name ?? '商品'} · ${item.line_id}`,
  }));
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
        {title: '意见', render: (_, item) => <>{item.notes || '—'}
          {user.role === 'support' && !!item.observations?.length && <Collapse size="small" ghost items={[{
            key: 'material-observations', label: '查看照片观察', children: <ul>
              {item.observations.map((observation) => {
                const material = detail.materials.find((photo) => photo.id === observation.material_id);
                return <li key={observation.material_id}>
                  {material ? materialName(material) : observation.material_id} · {findings[observation.finding]}
                  {observation.line_id && ` · ${scopedLines.find((line) => line.id === observation.line_id)?.name ?? observation.line_id}`}
                  {observation.affected_quantity != null && ` · 该商品行受影响总数 ${observation.affected_quantity} 件`}
                </li>;
              })}
            </ul>,
          }]} />}
        </>},
        {title: '记录时间', render: (_, item) => time(item.created_at)},
        {title: '适用材料', render: (_, item) => item.materials_version === detail.case.materials_version ? '当前版本' : '历史版本'},
      ]} />
    {editor && <MaterialEditor api={api} base={base} editor={editor} currentVersion={detail.case.version}
      currentMaterialsVersion={detail.case.materials_version} refresh={refresh}
      materials={detail.materials} lines={scopedLines} onPreview={setPreview}
      close={() => setEditor(undefined)} done={(message) => {setEditor(undefined); setNotice(message);}} />}
    {showPreview && <PhotoModal api={api} path={`${base}/materials/${segment(preview.id)}/content`} material={preview} close={() => setPreview(undefined)} />}
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

type ScopedLine = {id: string; quantity: number; name: string};

function reviewObservations(drafts: Record<string, ObservationDraft>, materials: Material[], lines: ScopedLine[]) {
  const observations: MaterialObservationInput[] = [];
  const counts = new Map<string, number>();
  for (const material of materials) {
    const draft = drafts[material.id];
    if (!draft?.finding) continue;
    const line = lines.find((item) => item.id === draft.line);
    const observation: MaterialObservationInput = {material_id: material.id, finding: draft.finding};
    if (line) observation.line_id = line.id;
    if (line && draft.finding === 'VISIBLE_DAMAGE' && draft.quantity?.trim()) {
      const value = Number(draft.quantity);
      if (!/^\d+$/.test(draft.quantity.trim()) || !Number.isSafeInteger(value) || value < 1 || value > line.quantity) {
        return {observations: [], error: `${line.name} 的受影响总数须为 1 至 ${line.quantity} 的整数。`};
      }
      if (counts.has(line.id) && counts.get(line.id) !== value) {
        return {observations: [], error: `${line.name} 在不同照片中填写的受影响总数须一致。`};
      }
      counts.set(line.id, value);
      observation.affected_quantity = value;
    }
    observations.push(observation);
  }
  return {observations, error: observations.length > 20 ? '每次最多记录 20 张照片的观察。' : ''};
}

function MaterialEditor({api, base, editor, currentVersion, currentMaterialsVersion, materials, lines, onPreview, refresh, close, done}: {
  api: Client; base: string; editor: Editor; currentVersion: number; currentMaterialsVersion: number; materials: Material[]; lines: ScopedLine[];
  onPreview: (material: Material) => void; refresh: () => void; close: () => void; done: (message: string) => void;
}) {
  const [file, setFile] = useState<File>();
  const [fileKey, setFileKey] = useState('');
  const [fileError, setFileError] = useState('');
  const [drafts, setDrafts] = useState<Record<string, ObservationDraft>>({});
  const actions = useActions(api);
  const upload = editor.kind === 'upload';
  const stale = currentVersion !== editor.version || (!upload && currentMaterialsVersion !== editor.materialsVersion);
  const {observations, error: observationError} = reviewObservations(drafts, materials, lines);
  function editObservation(id: string, patch: Partial<ObservationDraft>) {
    setDrafts((current) => ({...current, [id]: {...current[id], ...patch}}));
  }
  function selectFile(value: File | undefined) {
    if (value && value.size > 8 * 1024 * 1024) {
      setFile(undefined); setFileError('照片超过 8 MB，请压缩后重新选择。'); return;
    }
    setFile(value); setFileError(''); setFileKey(crypto.randomUUID());
  }
  return <Modal title={upload ? '上传证明照片' : '记录人工审核'} open footer={null} onCancel={actions.busy ? undefined : close}>
    <Form layout="vertical" initialValues={{kind: 'ITEM'}} onFinish={async (values: {kind?: string; decision?: string; notes?: string}) => {
      if (stale || observationError || (upload && !file)) return;
      const message = upload ? '照片已提交，当前审核状态请查看材料记录。' : '已记录本次人工审核。';
      if (upload && file) {
        const body = new FormData();
        body.set('kind', values.kind ?? 'ITEM');
        body.set('file', file);
        body.set('expected_version', String(editor.version));
        const result = await actions.submit(`${base}/materials`, body, {success: message, fingerprint: `${fileKey}:${values.kind}:${editor.version}`, refresh});
        if (result) done(message);
      } else {
        const result = await actions.submit(`${base}/material-reviews`, {
          expected_version: editor.version, decision: values.decision, notes: values.notes?.trim() ?? '',
          ...(observations.length > 0 && {observations}),
        }, {success: message, refresh});
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
        <Collapse size="small" items={[{key: 'observations', label: '照片观察（可选）', children: <div className="stack">
          <p>仅记录照片可见内容。未观察到可见破损时，商品实际状况仍可能需要核实。退款审批另行处理。</p>
          <p>同一商品行的多张照片可以佐证同一受影响总数，数量不可按照片相加。无法确认时可留空。</p>
          {materials.map((item, index) => {
            const draft = drafts[item.id] ?? {};
            const name = `照片 ${index + 1} · ${materialName(item)}`;
            const line = lines.find((entry) => entry.id === draft.line);
            return <fieldset key={item.id} disabled={stale || actions.busy}>
              <legend>{name}</legend>
              <Space orientation="vertical" className="full">
                <Button type="link" className="cell-link" onClick={() => onPreview(item)}>查看照片 {index + 1}</Button>
                <Select className="full" virtual={false} allowClear disabled={stale || actions.busy} aria-label={`照片 ${index + 1} 的观察结果`}
                  placeholder="观察结果" options={findingOptions} value={draft.finding}
                  onChange={(finding: MaterialFinding | undefined) => editObservation(item.id, finding ?
                    {finding, quantity: finding === 'VISIBLE_DAMAGE' ? draft.quantity : undefined} :
                    {finding: undefined, line: undefined, quantity: undefined})} />
                {draft.finding && !!lines.length && <Select className="full" virtual={false} allowClear disabled={stale || actions.busy}
                  aria-label={`照片 ${index + 1} 的对应商品行`} placeholder="对应商品行（可选）" value={draft.line}
                  options={lines.map((entry) => ({value: entry.id, label: entry.name}))}
                  onChange={(line: string | undefined) => editObservation(item.id, {line, quantity: undefined})} />}
                {line && draft.finding === 'VISIBLE_DAMAGE' && <label>
                  该商品行受影响总数（可选，最多 {line.quantity} 件）
                  <Input aria-label={`照片 ${index + 1} 的该商品行受影响总数`} inputMode="numeric" value={draft.quantity ?? ''}
                    onChange={(event) => editObservation(item.id, {quantity: event.target.value})} />
                </label>}
              </Space>
            </fieldset>;
          })}
        </div>}]} />
      </>}
      <Space orientation="vertical" className="full">
        {stale && <Alert type="warning" showIcon title="工单状态已更新。请关闭后查看当前材料，再重新提交。" />}
        <Feedback error={fileError || observationError || actions.error} />
        <Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale || !!observationError || (upload && !file)}>{upload ? '提交照片' : '保存审核结论'}</Button>
      </Space>
    </Form>
  </Modal>;
}
