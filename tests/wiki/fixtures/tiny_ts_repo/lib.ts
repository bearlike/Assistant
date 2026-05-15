import { readFile } from 'fs/promises';

export interface Loader {
  load(path: string): Promise<Buffer>;
}

export class FileLoader implements Loader {
  async load(path: string): Promise<Buffer> {
    return readFile(path);
  }
}

export class CachedLoader extends FileLoader {
  cache: Map<string, Buffer> = new Map();
  async load(path: string): Promise<Buffer> {
    return super.load(path);
  }
}
