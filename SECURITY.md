# Security

Written so a cyber security team could review how this tool handles inputs, data and dependencies.

## Threats considered and controls

| Threat | Control |
| --- | --- |
| Fetching from an unexpected host (e.g. a redirect or a poisoned listing) | Only `https://nemweb.com.au` and `www.nemweb.com.au` are allowed (`nemweb/client.py: check_url`) |
| Path traversal through remote file names | Names are reduced to their basename and must match `[A-Za-z0-9_.-]+` (`safe_name`) |
| Zip bombs or unexpected archive structure | Per-member size limit and a maximum nesting depth (`iter_csv_texts`) |
| Corrupted or tampered cache files | SHA-256 recorded on download and re-checked on reuse; mismatches are re-fetched |
| SQL injection | Values are bound as parameters; table names come from fixed definitions and are checked by `store.ident` |
| Vulnerable dependencies | `pip-audit` runs in CI on every push and pull request |
| Secrets in code | No credentials are needed. `.gitignore` excludes local config and data |
| Bad configuration | Settings are validated at start-up; unknown or invalid settings stop the run |

## Data sensitivity

All data is public market data from AEMO. No personal or confidential data is processed.

## Reporting a vulnerability

Please open a private security advisory on this repository (Security tab, "Report a vulnerability") rather than a public issue.
