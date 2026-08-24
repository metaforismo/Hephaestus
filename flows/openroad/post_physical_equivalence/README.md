# Post-physical equivalence support

The production implementation is in `src/hephaestus/post_physical_equivalence/` and runs from `.github/workflows/openroad-physical-evidence.yml`.

This directory retains the functional IHP SG13G2 cell definitions required by the production proof. The earlier monolithic experiment has been removed after the compositional implementation was tested and qualified.

The retained definitions describe two-state, zero-delay logic only. Timing, power, physical verification, and fabricated-device behavior remain outside their scope.
