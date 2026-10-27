import { describe, expect, it } from 'vitest';
import { formatBytes, formatDuration, uploadId } from './format';

describe('format', () => {
  it('formats sizes', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(1536)).toBe('1.5 KB');
    expect(formatBytes(5 * 1024 * 1024)).toBe('5.0 MB');
    expect(formatBytes(50 * 1024 * 1024)).toBe('50 MB');
  });

  it('formats durations', () => {
    expect(formatDuration(5 * 60000)).toBe('5 min');
    expect(formatDuration(125 * 60000)).toBe('2 h 5 min');
  });

  it('extracts the upload id from absolute and relative URLs', () => {
    expect(uploadId('https://example.com/files/3f2a?x=1')).toBe('3f2a');
    expect(uploadId('/files/abc')).toBe('abc');
  });
});
