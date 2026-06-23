# Minimizing smooth objectives

For smooth bowl-shaped (convex quadratic) objectives over continuous parameters:

- The metric decreases as you move parameters toward the basin of the minimum.
- Use the history: compare evaluated points and move in the direction that lowered the
  metric (an empirical gradient/coordinate-descent step).
- Take **moderate steps** — too large overshoots the minimum, too small converges slowly.
- When successive evaluations stop improving, you are near a stationary point.
