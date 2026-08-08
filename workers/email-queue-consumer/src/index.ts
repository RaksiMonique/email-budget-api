/**
 * Queue Consumer Worker: delivers email-received events to FastAPI.
 *
 * FastAPI processes SYNCHRONOUSLY and returns 200 only after the extraction is
 * committed (PLAN.md Phase 3) — so ack on 200 means "fully stored", and retry
 * on anything else gives at-least-once durability with no reaper.
 */

interface Env {
  FASTAPI_INTERNAL_URL: string;
  INTERNAL_SECRET: string;
}

interface EmailQueueMessage {
  email_id: string;
  alias_hash: string;
  r2_key: string;
  from: string;
  to: string;
  subject: string;
  message_id: string;
  date_header: string;
  received_at: string;
}

export default {
  async queue(batch: MessageBatch<EmailQueueMessage>, env: Env) {
    for (const message of batch.messages) {
      try {
        const res = await fetch(
          `${env.FASTAPI_INTERNAL_URL}/internal/email-received`,
          {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-Internal-Secret": env.INTERNAL_SECRET,
            },
            body: JSON.stringify(message.body),
            // Synchronous pipeline runs 1-5s, BUT free-tier Render spins down
            // after idle and cold-starts in ~30-60s — the timeout must outlast
            // a cold start or every first-email-after-idle burns a retry.
            signal: AbortSignal.timeout(75_000),
          },
        );
        if (res.ok) {
          message.ack();
        } else {
          message.retry();
        }
      } catch {
        message.retry();
      }
    }
  },
};
