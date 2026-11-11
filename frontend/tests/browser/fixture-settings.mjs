import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const requestedPort = process.env.AFTERCARE_FIXTURE_PORT ?? '5196';
export const port = Number(requestedPort);
if (!/^\d{4,5}$/.test(requestedPort) || port < 1024 || port > 65535) {
  throw new Error('AFTERCARE_FIXTURE_PORT must be a loopback port between 1024 and 65535.');
}
export const origin = `http://127.0.0.1:${port}`;
export const uiDist = resolve(process.env.AFTERCARE_UI_DIST ?? fileURLToPath(new URL('../../dist/', import.meta.url)));
