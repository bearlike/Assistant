import fs from 'fs';

export class Cache {
  constructor() { this.items = new Map(); }
  get(k) { return this.items.get(k); }
}

export class TimedCache extends Cache {
  expire(k) { this.items.delete(k); }
}

export function load(path) {
  return fs.readFileSync(path);
}

function helper() { return load('config.json'); }
