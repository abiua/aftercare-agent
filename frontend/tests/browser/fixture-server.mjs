import { access, readFile } from 'node:fs/promises';
import { createServer } from 'node:http';
import { extname, resolve, sep } from 'node:path';
import { origin, port, uiDist } from './fixture-settings.mjs';

const contentTypes = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
};

await access(resolve(uiDist, 'index.html'));
createServer(async (request, response) => {
  const pathname = new URL(request.url ?? '/', origin).pathname;
  if (request.method !== 'GET' && request.method !== 'HEAD') {
    response.writeHead(405).end();
    return;
  }
  if (pathname === '/__fixture_ready') {
    response.writeHead(204).end();
    return;
  }
  if (pathname.startsWith('/api/')) {
    response.writeHead(404).end();
    return;
  }
  const file = resolve(uiDist, pathname === '/' ? 'index.html' : `.${pathname}`);
  const contentType = contentTypes[extname(file)];
  if (!file.startsWith(`${uiDist}${sep}`) || !contentType) {
    response.writeHead(404).end();
    return;
  }
  try {
    const data = await readFile(file);
    response.writeHead(200, {'Content-Type': contentType, 'Cache-Control': 'no-store'});
    response.end(request.method === 'HEAD' ? undefined : data);
  } catch {
    response.writeHead(404).end();
  }
}).listen(port, '127.0.0.1');
