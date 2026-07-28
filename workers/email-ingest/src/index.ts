/**
 * Email Worker: receives inbound mail via the zone catch-all routing rule.
 *
 * Flow (docs/ingestion/cloudflare-email-setup.md):
 *   1. Reject anything not addressed to ALIAS_DOMAIN (apex typos keep their
 *      pre-existing rejected-at-SMTP behavior).
 *   2. Validate the alias token shape, then (when the API is live) validate the
 *      alias itself at the edge — fail-open on API errors, hard-reject on 404/410.
 *   3. Upload raw MIME to R2: emails/{alias}/{email_id}.eml
 *   4. Enqueue a lightweight message for the consumer worker.
 */

interface Env {
  R2_BUCKET: R2Bucket;
  EMAIL_QUEUE: Queue;
  ALIAS_DOMAIN: string;
  FASTAPI_INTERNAL_URL?: string;
  INTERNAL_SECRET?: string;
}

// secrets.token_urlsafe(12) yields 16 url-safe chars; accept a small range
// around that so token length can evolve without a worker redeploy.
const ALIAS_TOKEN_RE = /^[A-Za-z0-9_-]{8,64}$/;
const MAX_RAW_BYTES = 25 * 1024 * 1024; // matches R2/abuse guard in the docs

export default {
  async email(message: ForwardableEmailMessage, env: Env, ctx: ExecutionContext) {
    const recipient = message.to.toLowerCase();
    const atIndex = recipient.lastIndexOf("@");
    const local = recipient.slice(0, atIndex);
    const domain = recipient.slice(atIndex + 1);

    if (domain !== env.ALIAS_DOMAIN || !ALIAS_TOKEN_RE.test(local)) {
      message.setReject("Unknown address");
      return;
    }

    // Edge alias validation (defense-in-depth repeats in the webhook).
    // Fail-open when the API is unreachable/unset; reject only on explicit 404/410.
    if (env.FASTAPI_INTERNAL_URL && env.INTERNAL_SECRET) {
      try {
        const res = await fetch(
          `${env.FASTAPI_INTERNAL_URL}/internal/aliases/${local}`,
          {
            headers: { "X-Internal-Secret": env.INTERNAL_SECRET },
            signal: AbortSignal.timeout(5000),
          },
        );
        if (res.status === 404 || res.status === 410) {
          message.setReject("Unknown address");
          return;
        }
      } catch {
        // API down or timeout — accept; the webhook re-validates.
      }
    }

    if (message.rawSize > MAX_RAW_BYTES) {
      message.setReject("Message too large");
      return;
    }

    const emailId = crypto.randomUUID();
    const r2Key = `emails/${local}/${emailId}.eml`;
    const raw = await streamToArrayBuffer(message.raw);

    await env.R2_BUCKET.put(r2Key, raw, {
      httpMetadata: { contentType: "message/rfc822" },
      customMetadata: {
        alias: local,
        emailId,
        receivedAt: new Date().toISOString(),
      },
    });

    await env.EMAIL_QUEUE.send({
      email_id: emailId,
      alias_hash: local,
      r2_key: r2Key,
      from: message.from,
      to: recipient,
      subject: message.headers.get("subject") ?? "",
      message_id: message.headers.get("message-id") ?? "",
      date_header: message.headers.get("date") ?? "",
      received_at: new Date().toISOString(),
    });
  },
};

async function streamToArrayBuffer(stream: ReadableStream): Promise<ArrayBuffer> {
  const chunks: Uint8Array[] = [];
  const reader = stream.getReader();
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    chunks.push(value);
  }
  const total = chunks.reduce((sum, c) => sum + c.length, 0);
  const out = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    out.set(chunk, offset);
    offset += chunk.length;
  }
  return out.buffer;
}
