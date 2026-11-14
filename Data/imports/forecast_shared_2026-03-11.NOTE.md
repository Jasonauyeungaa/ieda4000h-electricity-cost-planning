# Historical pilot-era import — kept for provenance

`forecast_shared_2026-03-11.json` is a **historical pilot-era** forecast export. It is kept
only so the saved reproduction in `../outputs/shared_forecast_schedule_example.json` still
resolves.

- model: `cnn_2`
- model_version: `nsw1-pilot-ba198408c1db` — the **pilot-era** form
- Used by: nothing. Not `run_case.py`, not the two demo fixtures, not the README, not the
  skill, not the deck.

The current forecaster stamps **`m3_20261110T081713Z`**. That is the version any current run
or slide should quote. This file must never be cited as the current version.

**Why it is not regenerated.** Nothing reads it. Rebuilding it from the separate forecasting
project would create a fresh cross-project dependency for a file with no consumer, which is
worse than leaving a stale one in place.

**Do not modify this JSON.** Its SHA-256
(`27fc1e073525466a2ee09ee76a08ff15954bb606fe3e633b802ddc160e3cb4f9`) is recorded as
`input_sha256` in `../outputs/shared_forecast_schedule_example.json`. Any edit breaks that
provenance link. That is why this note is a separate file rather than a comment inside the
JSON.
