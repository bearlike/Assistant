import { describe, it, expect } from 'vitest';
import { hostOf, scopeTypeOf } from '../api/git';

describe('hostOf', () => {
  it('extracts the host from a full URL', () => {
    expect(hostOf('https://git.example.com/owner/repo.git')).toBe('git.example.com');
  });

  it('extracts the host from a bare slug', () => {
    expect(hostOf('git.example.com/owner/repo')).toBe('git.example.com');
  });

  it('extracts the host from scp-syntax (git@host:owner/repo)', () => {
    expect(hostOf('git@git.example.com:owner/repo.git')).toBe('git.example.com');
  });

  it('lowercases the host', () => {
    expect(hostOf('https://GIT.Example.COM/owner/repo')).toBe('git.example.com');
  });

  it('returns null for empty input', () => {
    expect(hostOf('  ')).toBeNull();
  });
});

describe('scopeTypeOf', () => {
  it('classifies a bare host as "host"', () => {
    expect(scopeTypeOf('git.example.com')).toBe('host');
  });

  it('classifies a host/owner/repo slug as "repo"', () => {
    expect(scopeTypeOf('git.example.com/owner/repo')).toBe('repo');
  });

  it('trims whitespace before classifying', () => {
    expect(scopeTypeOf('  git.example.com  ')).toBe('host');
    expect(scopeTypeOf('  git.example.com/owner/repo  ')).toBe('repo');
  });
});
