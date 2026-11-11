import type { ImportedReport } from './contracts';

export const maxReportBytes = 8 * 1024 * 1024;

type ReportIdentity = Pick<ImportedReport, 'id' | 'sha256'>;

export function reportImportNotice(sha256: string, previous: ReportIdentity[], returned: ReportIdentity) {
  const reused = returned.sha256 === sha256 && previous.some((record) =>
    record.id === returned.id && record.sha256 === sha256);
  return reused ? '已复用已有导入记录，首次登记的来源保持不变。' : '导入记录已保存，等待核验。';
}

export async function prepareReportImport(bytes: ArrayBuffer, origin: string) {
  if (bytes.byteLength > maxReportBytes) throw new Error('REPORT_TOO_LARGE');
  const source = origin.trim();
  if (!source || source.length > 200) throw new Error('INVALID_REPORT');
  const content = new TextDecoder('utf-8', {fatal: true, ignoreBOM: true}).decode(bytes);
  const parsed: unknown = JSON.parse(content);
  if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('INVALID_REPORT');
  const digest = await crypto.subtle.digest('SHA-256', bytes);
  const sha256 = Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('');
  return {content, sha256, origin: source};
}
