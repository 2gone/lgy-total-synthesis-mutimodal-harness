# Live five-paper routing gold

This operation can make paid OpenAI-compatible Chat Completions calls. It is never a CI command or part of the default test
suite. Adding this guide and its opt-in test does not authorize a real provider call.

## Prerequisites

Install the existing `router` extra, for example with `python -m pip install -e ".[router]"`. Set
only placeholder-configured `OPENAI_BASE_URL` (an absolute HTTPS URL ending in `/v1`) and
`OPENAI_API_KEY` in your local shell or credential manager; do not print or record either. The
fixed requested alias is `gemini-3.7-flash-high` and live behavior is selected only with `--live`.

Choose a persistent absolute work directory outside the repository. The test keeps cache and
recovery evidence in `.tsdaf-workspace` beneath that directory, so reuse the same directory for a
retry or rerun.

## Explicit command

Run only when a paid five-paper validation has been separately authorized:

```text
python -m pytest -p no:cacheprovider -q -s tests/live --run-live-gold --live-gold-workdir <absolute-path>
```

The test internally invokes the existing route CLI with the literal `--live` flag. It seals all
five predictions before loading the frozen gold, reuses the persistent cache on rerun, and archives
an immutable consolidated receipt plus a small receipt-ref pointer in the external work directory.

If a run fails partially or reports a retryable failure, preserve the workspace. Use
`python -m tsdaf run inspect <execution-id>` and, when appropriate,
`python -m tsdaf route resume <execution-id> --live` instead of deleting the workspace or changing
the gold or prompt.
