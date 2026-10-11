/* sse_progress.js — Generic SSE progress handler */
class SseProgress {
  constructor(url, options = {}) {
    this.url = url;
    this.body = options.body || null;
    this.method = options.method || 'POST';
    this.onProgress = options.onProgress || (() => {});
    this.onDone = options.onDone || (() => {});
    this.onError = options.onError || (() => {});
    this.onSelect = options.onSelect || (() => {});
    this.eventSource = null;
    this.abortController = null;
  }

  start() {
    this.abortController = new AbortController();

    // We use fetch + ReadableStream to support POST with SSE
    fetch(this.url, {
      method: this.method,
      headers: {
        'Content-Type': 'application/json',
        'Accept': 'text/event-stream',
      },
      body: this.body ? JSON.stringify(this.body) : null,
      signal: this.abortController.signal,
    }).then(async (response) => {
      if (!response.ok) {
        const text = await response.text();
        this.onError(new Error(`HTTP ${response.status}: ${text}`));
        return;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });

        // Parse SSE frames
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';
        let currentEvent = null;

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            currentEvent = line.slice(7).trim();
          } else if (line.startsWith('data: ')) {
            const data = line.slice(6);
            try {
              const parsed = JSON.parse(data);
              if (currentEvent === 'done') {
                this.onDone(parsed);
              } else if (currentEvent === 'error') {
                this.onError(new Error(parsed.message || 'Unknown error'));
              } else if (currentEvent === 'select') {
                this.onSelect(parsed);
              } else if (currentEvent === 'progress') {
                this.onProgress(parsed);
              }
            } catch (e) {
              // ignore parse errors for heartbeats etc
            }
            currentEvent = null;
          }
        }
      }
    }).catch((err) => {
      if (err.name !== 'AbortError') {
        this.onError(err);
      }
    });
  }

  stop() {
    if (this.abortController) {
      this.abortController.abort();
    }
  }
}
