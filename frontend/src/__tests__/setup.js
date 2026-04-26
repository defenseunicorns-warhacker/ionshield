import '@testing-library/jest-dom';

// Suppress Cesium-related console errors in test output
beforeAll(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
  vi.spyOn(console, 'warn').mockImplementation(() => {});
});

afterAll(() => {
  console.error.mockRestore?.();
  console.warn.mockRestore?.();
});
