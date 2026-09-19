"""Converters from public datasets into `eval/ground_truth/*.yaml`.

The harness scores a pipeline run against ground truth and does not care where
that ground truth came from -- a laser measure in someone's living room or a
public dataset's laser scan. These adapters produce the latter, which is what
makes the Phase 0 evaluation set free to assemble (implementation-plan.md 3.10,
Tier A).
"""
