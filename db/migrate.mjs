// db/migrate.mjs
// petal-anamnesis :: migration runner ✨
//
// usage:  node db/migrate.mjs
//
// scans db/migrations/*.sql in lexical order, applies any not yet recorded
// in schema_migrations~ idempotent, safe to run on every deploy.
import 'dotenv/config'

import Database from "better-sqlite3";
import { readFileSync, readdirSync } from "node:fs";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
const DB_PATH = process.env.PETAL_DB_PATH ?? join(__dirname, "petal-anamnesis.db");
// console.log("env db path: " + process.env.PETAL_DB_PATH);
const MIGRATIONS_DIR = join(__dirname, "schema/migrations");

const db = new Database(DB_PATH);
db.pragma("foreign_keys = ON");
db.pragma("journal_mode = WAL");

// bootstrap the bookkeeping table if this is a virgin db
db.exec(`
  CREATE TABLE IF NOT EXISTS schema_migrations (
    version    TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (datetime('now'))
  );
`);

const applied = new Set(
  db.prepare("SELECT version FROM schema_migrations").all().map((r) => r.version),
);

const files = readdirSync(MIGRATIONS_DIR)
  .filter((f) => f.endsWith(".sql"))
  .sort(); // lexical ordering handles 001_, 002_, ...

let count = 0;
for (const file of files) {
  const version = file.replace(/\.sql$/, "");
  if (applied.has(version)) continue;

  const sql = readFileSync(join(MIGRATIONS_DIR, file), "utf8");
  console.log(`✨ applying ${version}~`);

  const tx = db.transaction(() => {
    db.exec(sql);
    // migrations 002+ self-insert their version row; 001 already does it.
    // safety net for migrations that forget:
    db.prepare(
      "INSERT OR IGNORE INTO schema_migrations (version) VALUES (?)",
    ).run(version);
  });
  tx();
  count += 1;
}

if (count === 0) {
  console.log("🌸 db already current~");
} else {
  console.log(`💗 applied ${count} migration${count === 1 ? "" : "s"}~`);
}

db.close();
