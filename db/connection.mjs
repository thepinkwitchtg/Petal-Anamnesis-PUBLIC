// db/connection.mjs
// petal-anamnesis :: shared sqlite handle for api routes ✨
//
// pattern: one connection per process, lazily opened, WAL mode for
// concurrent readers~ astro api routes import { getDb } and reuse.

import 'dotenv/config'
import Database from "better-sqlite3";
import { join, dirname } from "node:path";
import { fileURLToPath } from "node:url";

const __dirname = dirname(fileURLToPath(import.meta.url));
// console.log("env db path: " + process.env.PETAL_DB_PATH);
const DB_PATH = process.env.PETAL_DB_PATH ?? join(__dirname, "petal-anamnesis.db");

let _db = null;

export function getDb() {
  if (_db) return _db;
  _db = new Database(DB_PATH);
  _db.pragma("foreign_keys = ON");
  _db.pragma("journal_mode = WAL");
  _db.pragma("synchronous = NORMAL"); // WAL-safe, faster writes
  return _db;
}

// helper for canonicalizing tag names (used at insert + lookup)
export function canonicalize(name) {
  return name.trim().toLowerCase().replace(/\s+/g, " ");
}
