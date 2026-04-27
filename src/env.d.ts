declare global {
  interface CloudflareEnv {
    KV: KVNamespace;
    WEBHOOK_SECRET?: string;
  }
}

export {};
