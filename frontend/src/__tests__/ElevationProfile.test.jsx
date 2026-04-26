/**
 * ElevationProfile.test.jsx
 *
 * Tests focus on three areas:
 *  1. Render nothing when < 2 waypoints (the previously-crashing early-return path)
 *  2. Correct total-distance label for a known route
 *  3. Segment risk colours when a decision payload is provided
 *
 * The critical regression: before the React-hooks fix the component violated
 * Rules of Hooks by early-returning BETWEEN two useMemo calls when
 * waypoints.length < 2, causing React error #310 (black screen) on the
 * second waypoint add. These tests guard against that regression.
 */

import { describe, it, expect } from 'vitest';
import { render, screen }       from '@testing-library/react';
import ElevationProfile         from '../components/ElevationProfile.jsx';

// ── Fixtures ──────────────────────────────────────────────────────────────────

/** Two waypoints roughly 111 km apart (1° latitude ≈ 111 km). */
const WP_PAIR = [
  { lat: 0,   lon: 0,   name: 'WP01' },
  { lat: 1,   lon: 0,   name: 'WP02' },
];

/** Three waypoints forming an L-shape. */
const WP_TRIPLE = [
  { lat: 0,   lon: 0,   name: 'WP01' },
  { lat: 1,   lon: 0,   name: 'WP02' },
  { lat: 1,   lon: 1,   name: 'WP03' },
];

const DECISION_CAUTION = {
  waypoints: [
    { risk_level: 'NOMINAL'  },
    { risk_level: 'CAUTION'  },
  ],
};

const DECISION_SEVERE = {
  waypoints: [
    { risk_level: 'ELEVATED' },
    { risk_level: 'SEVERE'   },
    { risk_level: 'DEGRADED' },
  ],
};

// ── Tests ─────────────────────────────────────────────────────────────────────

describe('ElevationProfile', () => {
  describe('with fewer than 2 waypoints', () => {
    it('renders nothing when waypoints is empty', () => {
      const { container } = render(
        <ElevationProfile waypoints={[]} decision={null} />,
      );
      expect(container.firstChild).toBeNull();
    });

    it('renders nothing with exactly 1 waypoint — no React hooks error', () => {
      // This is the regression test: before the fix, adding a 2nd waypoint
      // caused React error #310 because the component early-returned between
      // two useMemo calls. Rendering with 1 WP must NOT throw.
      const { container } = render(
        <ElevationProfile waypoints={[WP_PAIR[0]]} decision={null} />,
      );
      expect(container.firstChild).toBeNull();
    });

    it('renders correctly after transitioning from 1 to 2 waypoints (hook-order regression)', () => {
      // Re-render the same component instance with increasing waypoint counts —
      // this mirrors the exact sequence that triggered the black-screen crash.
      const { container, rerender } = render(
        <ElevationProfile waypoints={[]} decision={null} />,
      );
      expect(container.firstChild).toBeNull();

      rerender(<ElevationProfile waypoints={[WP_PAIR[0]]} decision={null} />);
      expect(container.firstChild).toBeNull();

      rerender(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      // Now we have 2 waypoints — the profile should render
      expect(container.firstChild).not.toBeNull();
    });
  });

  describe('with 2 waypoints and no decision', () => {
    it('renders the ROUTE PROFILE label', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      expect(screen.getByText('ROUTE PROFILE')).toBeTruthy();
    });

    it('shows total distance in km (≈111 km for 1° latitude)', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      // Distance label is "NNN km"; extract the number
      const distEl = document.querySelector('.elev-dist');
      expect(distEl).not.toBeNull();
      const km = parseInt(distEl.textContent, 10);
      // 1° latitude ≈ 111.19 km — allow ±2 km for floating-point
      expect(km).toBeGreaterThan(109);
      expect(km).toBeLessThan(113);
    });

    it('renders one segment bar between the two waypoints', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      const segs = document.querySelectorAll('.elev-seg');
      expect(segs.length).toBe(1);
    });

    it('uses the default blue colour when no decision is provided', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      const seg = document.querySelector('.elev-seg');
      expect(seg.style.background).toBe('rgb(59, 130, 246)'); // #3b82f6
    });

    it('renders two waypoint tick marks', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      const ticks = document.querySelectorAll('.elev-tick');
      expect(ticks.length).toBe(2);
    });

    it('shows the first and last waypoint names as labels', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      expect(screen.getByText('WP01')).toBeTruthy();
      expect(screen.getByText('WP02')).toBeTruthy();
    });
  });

  describe('with a decision payload', () => {
    it('renders 2 segments for 3-waypoint route', () => {
      render(<ElevationProfile waypoints={WP_TRIPLE} decision={DECISION_SEVERE} />);
      const segs = document.querySelectorAll('.elev-seg');
      expect(segs.length).toBe(2);
    });

    it('applies CAUTION risk colour to the segment', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={DECISION_CAUTION} />);
      const seg = document.querySelector('.elev-seg');
      // CAUTION → orange-ish; just verify it is NOT the default blue
      expect(seg.style.background).not.toBe('rgb(59, 130, 246)');
    });

    it('aria-label reflects total distance', () => {
      render(<ElevationProfile waypoints={WP_PAIR} decision={null} />);
      const el = document.querySelector('[role="img"]');
      expect(el.getAttribute('aria-label')).toMatch(/Route elevation profile/i);
      expect(el.getAttribute('aria-label')).toMatch(/km total/i);
    });
  });

  describe('edge cases', () => {
    it('handles coincident waypoints (zero distance) without crashing', () => {
      const identical = [
        { lat: 10, lon: 20, name: 'A' },
        { lat: 10, lon: 20, name: 'B' },
      ];
      // Zero-distance route should render null (total === 0, segments is [])
      const { container } = render(
        <ElevationProfile waypoints={identical} decision={null} />,
      );
      // Either renders nothing (segments=[]) or renders 0 km — just mustn't throw
      expect(true).toBe(true);
    });

    it('handles many waypoints (10) without crashing', () => {
      const manyWPs = Array.from({ length: 10 }, (_, i) => ({
        lat: i,
        lon: i,
        name: `WP${String(i + 1).padStart(2, '0')}`,
      }));
      expect(() =>
        render(<ElevationProfile waypoints={manyWPs} decision={null} />),
      ).not.toThrow();
    });
  });
});
