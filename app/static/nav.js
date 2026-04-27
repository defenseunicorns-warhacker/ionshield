/**
 * nav.js — Shared navigation + footer for IonShield marketing pages.
 * Injected via <script src="/static/nav.js"> in each page's <body>.
 * Marks the active nav link based on window.location.pathname.
 *
 * Served at /static/nav.js (copied from frontend/public/ by Vite build).
 */
(function () {
  'use strict';

  const LINKS = [
    { href: '/',           label: 'Home' },
    { href: '/features',   label: 'Features' },
    { href: '/simulation', label: 'Simulation' },
    { href: '/demo',       label: 'Demo' },
    { href: '/use-cases',  label: 'Use Cases' },
    { href: '/atak',       label: 'ATAK' },
    { href: '/docs',       label: 'Docs' },
    { href: '/pricing',    label: 'Pricing' },
    { href: '/compliance', label: 'Compliance' },
  ];

  const path = window.location.pathname.replace(/\/$/, '') || '/';

  // ── Navigation ──────────────────────────────────────────────────────────
  const navLinks = LINKS.map(l => {
    const active = (l.href === '/' ? path === '/' : path.startsWith(l.href));
    return `<a href="${l.href}" class="mkt-nav-link${active ? ' active' : ''}"
             aria-current="${active ? 'page' : 'false'}">${l.label}</a>`;
  }).join('');

  document.body.insertAdjacentHTML('afterbegin', `
    <nav class="mkt-nav" role="navigation" aria-label="Main navigation">
      <div class="mkt-nav-inner">
        <a href="/" class="mkt-logo" aria-label="IonShield home">
          <div class="mkt-logo-icon" aria-hidden="true">
            <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2"
                 stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="9"/>
              <path d="M3 12 Q12 6 21 12"/>
              <path d="M3 12 Q12 18 21 12"/>
              <line x1="12" y1="3" x2="12" y2="21"/>
            </svg>
          </div>
          <div>
            <div class="mkt-brand">IonShield</div>
            <div class="mkt-brand-sub">Space Weather Intelligence</div>
          </div>
        </a>
        <div class="mkt-nav-links">${navLinks}</div>
        <span class="mkt-nav-sep"></span>
        <a href="/dashboard" class="mkt-cta" aria-label="Open the live 3D dashboard">
          Launch Dashboard →
        </a>
      </div>
    </nav>
  `);

  // ── Footer ──────────────────────────────────────────────────────────────
  document.body.insertAdjacentHTML('beforeend', `
    <footer class="mkt-footer" role="contentinfo">
      <div class="mkt-footer-grid">
        <div class="mkt-footer-brand">
          <a href="/" class="mkt-logo" style="margin-bottom:10px;display:inline-flex;">
            <div class="mkt-logo-icon" style="width:28px;height:28px;border-radius:7px;" aria-hidden="true">
              <svg viewBox="0 0 24 24" fill="none" stroke="white" stroke-width="2.2" stroke-linecap="round" width="16" height="16">
                <circle cx="12" cy="12" r="9"/><path d="M3 12 Q12 6 21 12"/><path d="M3 12 Q12 18 21 12"/>
                <line x1="12" y1="3" x2="12" y2="21"/>
              </svg>
            </div>
            <div style="margin-left:8px;"><div class="mkt-brand">IonShield</div></div>
          </a>
          <p>Operational space weather intelligence for GPS, HF comms, and route risk assessment. Built on real-time NOAA SWPC data with physics-based models.</p>
        </div>
        <div class="mkt-footer-col">
          <h4>Product</h4>
          <a href="/features">Features</a>
          <a href="/demo">Live Demo</a>
          <a href="/pricing">Pricing & Pilot</a>
          <a href="/dashboard">Dashboard</a>
        </div>
        <div class="mkt-footer-col">
          <h4>Resources</h4>
          <a href="/docs">Documentation</a>
          <a href="/docs#api">API Reference</a>
          <a href="/docs#faq">FAQ</a>
          <a href="/use-cases">Use Cases</a>
        </div>
        <div class="mkt-footer-col">
          <h4>Company</h4>
          <a href="/compliance">Security & Compliance</a>
          <a href="/api/status" target="_blank" rel="noopener">Live Status JSON</a>
          <a href="/docs" target="_blank" rel="noopener">API Docs (Swagger)</a>
        </div>
      </div>
      <div class="mkt-footer-bottom">
        <span>© 2026 IonShield. All rights reserved.</span>
        <span>
          <a href="/compliance">Privacy</a> ·
          <a href="/compliance">DFARS/ITAR Compliance</a> ·
          <a href="/compliance">Security</a>
        </span>
      </div>
      <p class="mkt-footer-legal" style="max-width:1200px;margin:12px auto 0;padding:0 0 16px;">
        Data sourced from NOAA Space Weather Prediction Center (public domain).
        For operational decision support only — verify against current NOAA advisories before mission execution.
        IonShield outputs are not certified for sole-source navigation or safety-of-life applications.
        EAR99 / Not ITAR-controlled. FedRAMP readiness in progress.
      </p>
    </footer>
  `);

  // ── Google Analytics (GA4 placeholder) ─────────────────────────────────
  // TODO: Replace G-XXXXXXXXXX with your actual GA4 measurement ID
  const GA_ID = 'G-XXXXXXXXXX';
  if (GA_ID !== 'G-XXXXXXXXXX' && location.hostname !== 'localhost') {
    const s = document.createElement('script');
    s.src = `https://www.googletagmanager.com/gtag/js?id=${GA_ID}`;
    s.async = true;
    document.head.appendChild(s);
    window.dataLayer = window.dataLayer || [];
    function gtag() { window.dataLayer.push(arguments); }
    gtag('js', new Date());
    gtag('config', GA_ID, { anonymize_ip: true });
  }
})();
