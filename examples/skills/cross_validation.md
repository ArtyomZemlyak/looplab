---
name: cross_validation
description: How to evaluate a model honestly with K-fold cross-validation
---

# Cross-validation

To estimate generalization error without leakage:

1. Split the data into K folds (typically K=5).
2. For each fold: train on the other K−1 folds, evaluate on the held-out fold.
3. Report the **mean** of the per-fold errors as the metric.

Pitfalls to avoid:
- Never fit any preprocessing (scaling, feature selection) on the full dataset before
  splitting — fit it inside each training fold only, or you leak test information.
- For time series, use forward-chaining / purged walk-forward splits, not random K-fold.
