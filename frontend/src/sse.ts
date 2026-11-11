export type SseFrame = {id: string; event: string; data: string};

export async function readSse(stream: ReadableStream<Uint8Array>, onFrame: (frame: SseFrame) => void) {
  const reader = stream.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  let id = '';
  let event = '';
  let data: string[] = [];
  const consume = (line: string) => {
    if (line === '') {
      if (data.length) onFrame({id, event, data: data.join('\n')});
      event = '';
      data = [];
      return;
    }
    if (line.startsWith(':')) return;
    const separator = line.indexOf(':');
    const field = separator < 0 ? line : line.slice(0, separator);
    const value = separator < 0 ? '' : line.slice(separator + 1).replace(/^ /, '');
    if (field === 'data') data.push(value);
    if (field === 'event') event = value;
    if (field === 'id' && !value.includes('\0')) id = value;
  };
  try {
    for (;;) {
      const {value, done} = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, {stream: true});
      let end = buffer.search(/[\r\n]/);
      while (end !== -1 && !(end === buffer.length - 1 && buffer[end] === '\r')) {
        consume(buffer.slice(0, end));
        buffer = buffer.slice(end + (buffer.slice(end, end + 2) === '\r\n' ? 2 : 1));
        end = buffer.search(/[\r\n]/);
      }
    }
    if (buffer.endsWith('\r')) consume(buffer.slice(0, -1));
  } finally {
    await reader.cancel().catch(() => undefined);
    reader.releaseLock();
  }
}
