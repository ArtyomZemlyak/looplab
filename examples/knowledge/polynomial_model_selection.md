# Polynomial degree & ridge model selection

Choosing model complexity is a bias–variance tradeoff:

- **Too low a degree underfits** (high bias): the model can't capture the signal, so
  both training and cross-validation error stay high.
- **Too high a degree overfits** (high variance): training error keeps dropping but
  cross-validation (held-out) error rises as the model chases noise.
- The **optimal polynomial degree matches the data's true generating degree**. Use
  K-fold cross-validation and pick the degree that minimizes the mean held-out MSE.

Practical guidance:

- Start near a low-to-moderate degree (2–3) and adjust based on CV error.
- **Ridge regularization (lambda > 0)** shrinks coefficients and helps when the degree
  is slightly too high — prefer a small lambda before increasing the degree further.
- When CV MSE has plateaued near the data's noise floor, stop increasing complexity.
