/**
 * riskColors.test.js
 * Tests for the risk/action colour palette utilities.
 */

import { describe, it, expect } from 'vitest';
import {
  getRiskStyle,
  getActionStyle,
  confScoreColor,
  confLabelText,
  RISK,
  ACTION,
} from '../utils/riskColors.js';

describe('getRiskStyle', () => {
  it('returns green for NOMINAL', () => {
    expect(getRiskStyle('NOMINAL').color).toBe('#10B981');
  });
  it('returns red for SEVERE', () => {
    expect(getRiskStyle('SEVERE').color).toBe('#EF4444');
  });
  it('returns orange for DEGRADED', () => {
    expect(getRiskStyle('DEGRADED').color).toBe('#F97316');
  });
  it('falls back to NOMINAL for unknown level', () => {
    expect(getRiskStyle('UNKNOWN')).toBe(RISK.NOMINAL);
  });
  it('every level has a rgba array for Cesium', () => {
    Object.values(RISK).forEach(v => {
      expect(v.rgba).toHaveLength(4);
      v.rgba.forEach(n => expect(n).toBeGreaterThanOrEqual(0));
    });
  });
});

describe('getActionStyle', () => {
  it('returns green for GO', () => {
    expect(getActionStyle('GO').color).toBe('#10B981');
  });
  it('returns red for NO_GO', () => {
    expect(getActionStyle('NO_GO').color).toBe('#EF4444');
  });
  it('returns red for HF_NOT_VIABLE', () => {
    expect(getActionStyle('HF_NOT_VIABLE').color).toBe('#EF4444');
  });
  it('returns orange for DEGRADED_MODE', () => {
    expect(getActionStyle('DEGRADED_MODE').color).toBe('#F97316');
  });
  it('returns human-readable label for unknown action', () => {
    const s = getActionStyle('SOME_UNKNOWN_ACTION');
    expect(s.label).toBe('SOME UNKNOWN ACTION');
  });
  it('all defined actions have a label string', () => {
    Object.values(ACTION).forEach(a => {
      expect(typeof a.label).toBe('string');
      expect(a.label.length).toBeGreaterThan(0);
    });
  });
});

describe('confScoreColor', () => {
  it('returns green for high confidence (≥0.75)', () => {
    expect(confScoreColor(0.9)).toBe('#10B981');
    expect(confScoreColor(0.75)).toBe('#10B981');
  });
  it('returns yellow for medium confidence (0.55–0.74)', () => {
    expect(confScoreColor(0.65)).toBe('#F59E0B');
    expect(confScoreColor(0.55)).toBe('#F59E0B');
  });
  it('returns red for low confidence (<0.55)', () => {
    expect(confScoreColor(0.3)).toBe('#EF4444');
    expect(confScoreColor(0)).toBe('#EF4444');
  });
  it('boundary: 0.74 is medium', () => {
    expect(confScoreColor(0.74)).toBe('#F59E0B');
  });
});

describe('confLabelText', () => {
  it('maps HIGH correctly', () => expect(confLabelText('HIGH')).toBe('HIGH'));
  it('maps VERY_LOW to VERY LOW', () => expect(confLabelText('VERY_LOW')).toBe('VERY LOW'));
  it('returns the raw value for unknown labels', () => expect(confLabelText('CUSTOM')).toBe('CUSTOM'));
  it('returns — for null/undefined', () => expect(confLabelText(null)).toBe('—'));
});
