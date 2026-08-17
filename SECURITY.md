# Security policy

## Supported versions

Hephaestus is pre-alpha. Security fixes are applied to the current `main` branch.

## Reporting

Please report vulnerabilities privately through GitHub Security Advisories for this repository.
Do not open a public issue before a fix or disclosure plan exists.

## Threat model highlights

Hephaestus processes local model and calibration files. The frontend intentionally:

- disables NumPy pickle deserialization;
- does not execute remote model code;
- validates Safetensors index structure;
- rejects absolute and directory-escaping shard paths;
- materializes only explicit tensor selections;
- writes generated artifacts only beneath the user-selected output directory.

Safetensors protects against code execution through pickle, not against resource exhaustion,
maliciously huge dimensions, untrusted filesystem races, or all parser/library vulnerabilities.
Run untrusted checkpoints with OS-level resource limits and isolation.

Proprietary PDKs, foundry rule decks, standard-cell libraries, credentials, and NDA material must
never be committed to this public repository or uploaded to public CI.
