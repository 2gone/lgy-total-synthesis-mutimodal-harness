# Live five-paper routing gold

This operation can make paid OpenAI-compatible Chat Completions calls. It is never a CI command or part of the default test suite. Its presence, the opt-in test, and any past result do not authorize a real provider call.

## Prerequisites

Obtain explicit authorization for this particular run. Install the existing `router` extra, for example with `python -m pip install -e ".[router]"`. In a local shell or credential manager, set nonblank `OPENAI_API_KEY` and a safe `OPENAI_BASE_URL`; never print or record either. For article routing the URL must be absolute, end exactly in `/v1`, have no userinfo/query/fragment, and be either HTTPS or HTTP for only the literal loopback hosts `127.0.0.1`, `localhost`, or `::1`. The fixed requested alias is `gemini-3.7-flash-high`; live behavior is selected only with `--live`.

Choose a persistent absolute work directory outside the repository. The test keeps cache and recovery evidence in `.tsdaf-workspace` beneath that directory, so reuse the same directory for a retry or rerun.

## Explicit command

Run only after the separate authorization described above:

```text
python -m pytest -p no:cacheprovider -q -s tests/live --run-live-gold --live-gold-workdir <absolute-path>
```

The test internally invokes the existing route CLI with the literal `--live` flag. It seals all five predictions before loading the frozen gold, reuses the persistent cache on rerun, and archives an immutable consolidated receipt plus a small receipt-ref pointer in the external work directory.

If a run fails partially or reports a retryable failure, preserve the workspace. Use `python -m tsdaf run inspect <execution-id>` and, when appropriate, `python -m tsdaf route resume <execution-id> --live` instead of deleting the workspace or changing the gold or prompt.

## Historical completed evidence

The first local, explicit-live five-paper validation completed successfully: `1 passed in 260.30s`. All 37 visual decisions matched frozen gold with zero escalation; the nine original assets had the exact `3/2/1/2/1` distribution. The receipt artifact SHA-256 was `05c3b902035008218c1f1fec722a7db9e4dc5df801b1facc303a082af42a5019`.

This is completed evidence, not standing authorization to repeat the paid operation. It validates article routing only and does not validate real-weight/GPU image processing or a live Gemini OCSR canary.
