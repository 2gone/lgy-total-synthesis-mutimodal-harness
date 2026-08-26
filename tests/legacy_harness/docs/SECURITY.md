# Credential and gateway safety

- Keep the repository private.
- Store gateway values only in an untracked local `.env` or an approved secret manager.
- Never commit `.env`, bearer headers, request dumps containing authorization headers, or gateway hostnames.
- GitHub Actions deliberately has no model credential and performs no paid call.
- Run metadata contains fixed model parameters, input hashes, response status, and timings; it excludes the configured API base and key.
- If credentials appear in a commit, rotate them before rewriting history or closing access.

The controller-only reference directory is not a secret store, but opening it before all generation cells are sealed can bias the experiment. Treat it as restricted experimental information and keep its CODEOWNERS review boundary.
