/**
 * Panel.test.jsx
 * Tests for Panel sub-components that do not depend on CesiumJS.
 * Globe.jsx is mocked so jsdom doesn't need WebGL.
 */

import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DecisionResult, ConfidenceSection, WaypointList } from '../components/Panel.jsx';

// Mock Globe so it never touches Cesium in these tests
vi.mock('../components/Globe.jsx', () => ({
  default: vi.fn(() => <div data-testid="globe-mock" />),
}));

// ── Fixtures ──────────────────────────────────────────────────────────────────

const CONFIDENCE_HIGH = {
  score:               0.82,
  label:               'HIGH',
  stale_data:          false,
  stale_penalty_applied: false,
  data_completeness:   1.0,
  drivers: [
    { factor: 'data_freshness', effect: 0.15, detail: 'Data is recent' },
    { factor: 'bz_variability', effect: -0.03, detail: 'Mild Bz variation' },
  ],
};

const CONFIDENCE_STALE = {
  score:               0.38,
  label:               'LOW',
  stale_data:          true,
  stale_penalty_applied: true,
  data_completeness:   0.67,
  drivers: [
    { factor: 'data_freshness', effect: -0.35, detail: 'Data is 15 min old' },
  ],
};

const ROUTE_DECISION = {
  action:           'CAUTION',
  action_sentence:  'Elevated Kp and negative Bz indicate moderate ionospheric risk along this route.',
  valid_until:      '2024-05-11T19:00:00Z',
  alternatives:     ['DELAY_OPERATION', 'ALTERNATE_ROUTE'],
  recommended_actions: [
    'Enable backup navigation.',
    'Verify SATCOM link before departure.',
  ],
  confidence: CONFIDENCE_HIGH,
  impacts: [
    { system: 'GPS', metric: 'max_gps_error_m', value: 45, detail: 'GPS error elevated at polar waypoints.' },
  ],
  provenance: {
    model_version:      '1.0.0',
    input_hash:         'sha256:a3f9c1d2e4b5f6a7b8c9d0e1f2a3b4c5d6e7f8a9',
    computed_at:        '2024-05-11T18:00:00Z',
    observations_used:  ['kp_index', 'bz_gsm_nt', 'xray_flux_wm2'],
    forecasts_used:     ['kp_forecast_24h=5.3'],
    feeds_unavailable:  [],
  },
  waypoints: [
    { name: 'WP01', lat: 38.8, lon: -77.0, risk_level: 'ELEVATED', gps_error_m: 35, hf_viable: true,  pca_active: false },
    { name: 'WP02', lat: 40.0, lon: -75.0, risk_level: 'CAUTION',  gps_error_m: 45, hf_viable: false, pca_active: false },
  ],
};

// ── DecisionResult ────────────────────────────────────────────────────────────

describe('DecisionResult', () => {
  it('renders nothing when decision is null', () => {
    const { container } = render(<DecisionResult decision={null} />);
    expect(container.firstChild).toBeNull();
  });

  it('shows the action badge label', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    // Use the data-testid container and check for the badge element within it
    const panel = screen.getByTestId('decision-result');
    const badge = panel.querySelector('.dec-action-badge');
    expect(badge).toHaveTextContent('CAUTION');
  });

  it('shows the action_sentence in plain text', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    expect(screen.getByTestId('action-sentence')).toHaveTextContent(
      'Elevated Kp and negative Bz indicate moderate ionospheric risk',
    );
  });

  it('does NOT show stale banner when data is fresh', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    expect(screen.queryByText(/STALE DATA/)).toBeNull();
  });

  it('shows STALE DATA banner when confidence.stale_data is true', () => {
    const staleDecision = { ...ROUTE_DECISION, confidence: CONFIDENCE_STALE };
    render(<DecisionResult decision={staleDecision} />);
    expect(screen.getByText(/STALE DATA/)).toBeInTheDocument();
  });

  it('shows alternative actions', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    expect(screen.getByText(/DELAY OPERATION/i)).toBeInTheDocument();
  });

  it('renders all recommended actions', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    expect(screen.getByText('Enable backup navigation.')).toBeInTheDocument();
    expect(screen.getByText('Verify SATCOM link before departure.')).toBeInTheDocument();
  });

  it('shows impact system label', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    expect(screen.getByText('GPS')).toBeInTheDocument();
  });

  it('renders the provenance section', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    // Provenance is in a <details> — just check the summary text is present
    expect(screen.getByText(/Provenance/i)).toBeInTheDocument();
  });

  it('shows waypoint risk table', () => {
    render(<DecisionResult decision={ROUTE_DECISION} />);
    // WP labels appear both in the new TranslationSummary "legs affected"
    // chips and in the per-waypoint table, so we use getAllByText.
    expect(screen.getAllByText('WP01').length).toBeGreaterThan(0);
    expect(screen.getAllByText('WP02').length).toBeGreaterThan(0);
  });
});

// ── ConfidenceSection ─────────────────────────────────────────────────────────

describe('ConfidenceSection', () => {
  it('displays score and label', () => {
    render(<ConfidenceSection confidence={CONFIDENCE_HIGH} />);
    expect(screen.getByText(/HIGH/)).toBeInTheDocument();
    expect(screen.getByText(/0\.82/)).toBeInTheDocument();
  });

  it('renders positive driver effect with + sign', () => {
    render(<ConfidenceSection confidence={CONFIDENCE_HIGH} />);
    expect(screen.getByText('+0.15')).toBeInTheDocument();
  });

  it('renders negative driver effect', () => {
    render(<ConfidenceSection confidence={CONFIDENCE_HIGH} />);
    expect(screen.getByText('-0.03')).toBeInTheDocument();
  });

  it('shows data completeness', () => {
    render(<ConfidenceSection confidence={CONFIDENCE_HIGH} />);
    expect(screen.getByText(/100%/)).toBeInTheDocument();
  });

  it('shows stale penalty note when applied', () => {
    render(<ConfidenceSection confidence={CONFIDENCE_STALE} />);
    expect(screen.getByText(/stale penalty applied/)).toBeInTheDocument();
  });
});

// ── WaypointList ──────────────────────────────────────────────────────────────

describe('WaypointList', () => {
  it('shows empty message when no waypoints', () => {
    render(<WaypointList waypoints={[]} decision={null} onRemove={() => {}} />);
    expect(screen.getByText(/No waypoints yet/)).toBeInTheDocument();
  });

  it('renders waypoint names', () => {
    const wps = [
      { name: 'Alpha Base', lat: 38.8, lon: -77.0 },
      { name: 'Bravo Post', lat: 39.0, lon: -76.5 },
    ];
    render(<WaypointList waypoints={wps} decision={null} onRemove={() => {}} />);
    expect(screen.getByText('Alpha Base')).toBeInTheDocument();
    expect(screen.getByText('Bravo Post')).toBeInTheDocument();
  });

  it('calls onRemove with correct index when remove button clicked', () => {
    const onRemove = vi.fn();
    const wps = [{ name: 'WP01', lat: 0, lon: 0 }];
    render(<WaypointList waypoints={wps} decision={null} onRemove={onRemove} />);
    fireEvent.click(screen.getByRole('button', { name: /Remove waypoint WP01/ }));
    expect(onRemove).toHaveBeenCalledWith(0);
  });

  it('shows risk-level dot when decision data is available', () => {
    const decision = {
      waypoints: [{ risk_level: 'SEVERE', gps_error_m: 80, hf_viable: false }],
    };
    const wps = [{ name: 'WP01', lat: 38.8, lon: -77.0 }];
    render(<WaypointList waypoints={wps} decision={decision} onRemove={() => {}} />);
    // The coloured dot has a title attribute with the risk level
    const dot = screen.getByTitle('SEVERE');
    expect(dot).toBeInTheDocument();
  });
});
