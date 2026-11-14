---
name: compare-ai-electricity-cost
description: Compare and independently verify electricity expense for flexible GPU jobs using a validated NSW1 forecast.
---

# Compare GPU schedules and electricity cost

Use this Skill to prepare a daily electricity budget for the declared GPU site in this package. Work from `Project` with the dependencies from the repository README installed.

1. Pick a NSW1 forecast JSON conforming to `electricity.forecast.v2`. Check the origin, 48 consecutive half-hour targets, UTC+10 and AUD/MWh. Put new inputs in `Data/imports`.
2. For the changed case, run `python optimize.py --input imports/forecast_aac8fe2222acaca756808516.json --scenario more-work --tariff linked`. Use the Python executable from the project's environment. Select another documented case or forecast when requested.
3. Read the output path from stdout and open that JSON. Verify both independent verdicts. Confirm the jobs met their windows and the constraints hold. Expect 37.5 GPU-hours for this case. Show the forecast budget, observed-price comparison and the result path. If the importer rejects the packet or a checker fails, report the error and stop before interpreting costs. If observed prices are incomplete, mark the replay pending.
4. Explain which site and tariff assumptions were used. If a fixed-volume CfD was requested, report its payoff and the resulting net cost separately.

[openJiuwen JiuwenSwarm](https://github.com/openJiuwen-ai/jiuwenswarm)
