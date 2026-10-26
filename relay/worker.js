// AutoStudio website + phone relay (Cloudflare Worker).
//
// Static files in site/ are served as before. /relay/v1/<box>/... is a mailbox between one PC and its
// paired phones, for when a phone is not on the PC's network (see autostudio/relay.py):
//   POST register  (PC)     {devices: [{id, token_sha256}]}  who may read this mailbox; the first call claims it
//   POST out       (PC)     {items: [{to, body}]}            messages for phones
//   GET  out       (phone)  ?device=ID&after=SEQ             that phone's messages
//   POST in        (phone)  {body}                           an answer for the PC
//   GET  in        (PC)     ?after=SEQ                       answers; everything up to SEQ is deleted
// Bodies are sealed end to end (AES-256-GCM, keys exchanged on the local network at pairing): this
// worker cannot read them. The PC authenticates with its key, each phone with its own token; only
// SHA-256 hashes of those are stored. A mailbox nobody has touched for 30 days deletes itself.

import { DurableObject } from "cloudflare:workers";

const BOX = /^\/relay\/v1\/([0-9a-f]{32})\/(register|out|in)$/;
const MAX_BODY = 384 * 1024;        // a message carries at most a few small cover thumbnails
const MAX_ITEMS = 40;
const KEEP_PER_PHONE = 100;
const KEEP_MS = 7 * 86400e3;
const IDLE_MS = 30 * 86400e3;
const MAX_DEVICES = 20;

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), { status, headers: { "content-type": "application/json", "cache-control": "no-store" } });

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const m = url.pathname.match(BOX);
    if (!m) {
      if (url.pathname.startsWith("/relay/")) return json({ error: "not found" }, 404);
      return env.ASSETS.fetch(req);
    }
    const len = Number(req.headers.get("content-length") || 0);
    if (len > MAX_BODY * 2) return json({ error: "too large" }, 413);
    return env.MAILBOX.get(env.MAILBOX.idFromName(m[1])).fetch(req);
  },
};

async function sha256hex(text) {
  const d = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(d)].map((b) => b.toString(16).padStart(2, "0")).join("");
}

function same(a, b) {
  const x = new TextEncoder().encode(a || "");
  const y = new TextEncoder().encode(b || "");
  return x.length === y.length && crypto.subtle.timingSafeEqual(x, y);
}

function bearer(req) {
  const h = req.headers.get("authorization") || "";
  return h.toLowerCase().startsWith("bearer ") ? h.slice(7).trim() : "";
}

export class Mailbox extends DurableObject {
  constructor(ctx, env) {
    super(ctx, env);
    this.sql = ctx.storage.sql;
    this.sql.exec(`
      CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
      CREATE TABLE IF NOT EXISTS devices (id TEXT PRIMARY KEY, hash TEXT NOT NULL);
      CREATE TABLE IF NOT EXISTS msgs (seq INTEGER PRIMARY KEY AUTOINCREMENT, dir TEXT NOT NULL, peer TEXT NOT NULL,
                                       body TEXT NOT NULL, ts INTEGER NOT NULL);
      CREATE INDEX IF NOT EXISTS msgs_dir ON msgs(dir, peer, seq);`);
  }

  meta(k) {
    const r = this.sql.exec("SELECT v FROM meta WHERE k = ?", k).toArray();
    return r.length ? r[0].v : null;
  }

  async isPc(req) {
    const key = bearer(req);
    const want = this.meta("pc_hash");
    return !!key && !!want && same(await sha256hex(key), want);
  }

  async device(req, id) {
    const token = bearer(req);
    if (!token || !id) return null;
    const r = this.sql.exec("SELECT hash FROM devices WHERE id = ?", id).toArray();
    return r.length && same(await sha256hex(token), r[0].hash) ? id : null;
  }

  async touch() {
    const now = Date.now();
    const at = await this.ctx.storage.getAlarm();
    if (!at || at < now + IDLE_MS - 86400e3) await this.ctx.storage.setAlarm(now + IDLE_MS);
  }

  async alarm() {
    await this.ctx.storage.deleteAll();     // forgotten: nobody used this mailbox for a month
  }

  async fetch(req) {
    const url = new URL(req.url);
    const what = url.pathname.match(BOX)[2];
    const now = Date.now();
    let body = null;
    if (req.method === "POST") {
      const text = await req.text();
      if (text.length > MAX_BODY) return json({ error: "too large" }, 413);
      try { body = JSON.parse(text || "{}"); } catch { return json({ error: "bad json" }, 400); }
    }

    if (what === "register" && req.method === "POST") {
      const key = bearer(req);
      if (key.length < 20) return json({ error: "key" }, 400);
      const hash = await sha256hex(key);
      const have = this.meta("pc_hash");
      if (have && !same(hash, have)) return json({ error: "forbidden" }, 403);
      if (!have) this.sql.exec("INSERT INTO meta (k, v) VALUES ('pc_hash', ?)", hash);
      const devs = (Array.isArray(body.devices) ? body.devices : []).slice(0, MAX_DEVICES)
        .filter((d) => d && /^[\w-]{1,40}$/.test(d.id || "") && /^[0-9a-f]{64}$/.test(d.token_sha256 || ""));
      this.sql.exec("DELETE FROM devices");
      for (const d of devs) this.sql.exec("INSERT INTO devices (id, hash) VALUES (?, ?)", d.id, d.token_sha256);
      // messages for phones that were disconnected go too
      this.sql.exec(`DELETE FROM msgs WHERE peer NOT IN (SELECT id FROM devices)`);
      await this.touch();
      return json({ ok: true, devices: devs.length });
    }

    if (what === "out" && req.method === "POST") {
      if (!(await this.isPc(req))) return json({ error: "forbidden" }, 403);
      const items = (Array.isArray(body.items) ? body.items : []).slice(0, MAX_ITEMS);
      const known = new Set(this.sql.exec("SELECT id FROM devices").toArray().map((r) => r.id));
      let n = 0;
      for (const it of items) {
        if (!it || !known.has(it.to) || typeof it.body !== "string") continue;
        this.sql.exec("INSERT INTO msgs (dir, peer, body, ts) VALUES ('out', ?, ?, ?)", it.to, it.body, now);
        n++;
      }
      this.sql.exec("DELETE FROM msgs WHERE ts < ?", now - KEEP_MS);
      for (const id of known) {
        this.sql.exec(`DELETE FROM msgs WHERE dir = 'out' AND peer = ? AND seq NOT IN
                       (SELECT seq FROM msgs WHERE dir = 'out' AND peer = ? ORDER BY seq DESC LIMIT ?)`, id, id, KEEP_PER_PHONE);
      }
      await this.touch();
      return json({ ok: true, stored: n });
    }

    if (what === "out" && req.method === "GET") {
      const id = await this.device(req, url.searchParams.get("device") || "");
      if (!id) return json({ error: "forbidden" }, 403);
      const after = Number(url.searchParams.get("after") || 0) || 0;
      const items = this.sql.exec("SELECT seq, body, ts FROM msgs WHERE dir = 'out' AND peer = ? AND seq > ? ORDER BY seq LIMIT 30",
        id, after).toArray();
      const last = this.sql.exec("SELECT MAX(seq) AS m FROM msgs").toArray()[0].m || 0;
      return json({ items, last });
    }

    if (what === "in" && req.method === "POST") {
      const id = await this.device(req, req.headers.get("x-device") || "");
      if (!id) return json({ error: "forbidden" }, 403);
      if (typeof body.body !== "string" || body.body.length > 64 * 1024) return json({ error: "bad body" }, 400);
      const queued = this.sql.exec("SELECT COUNT(*) AS n FROM msgs WHERE dir = 'in' AND peer = ?", id).toArray()[0].n;
      if (queued >= 100) return json({ error: "full" }, 429);
      this.sql.exec("INSERT INTO msgs (dir, peer, body, ts) VALUES ('in', ?, ?, ?)", id, body.body, now);
      await this.touch();
      return json({ ok: true });
    }

    if (what === "in" && req.method === "GET") {
      if (!(await this.isPc(req))) return json({ error: "forbidden" }, 403);
      const after = Number(url.searchParams.get("after") || 0) || 0;
      if (after) this.sql.exec("DELETE FROM msgs WHERE dir = 'in' AND seq <= ?", after);
      const items = this.sql.exec("SELECT seq, peer AS \"from\", body, ts FROM msgs WHERE dir = 'in' AND seq > ? ORDER BY seq LIMIT 50",
        after).toArray();
      return json({ items });
    }

    return json({ error: "method" }, 405);
  }
}
