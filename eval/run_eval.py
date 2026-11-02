"""`python -m eval.run_eval --config all`

Runs only on documents that have labels in data/labels/. Configs:
  small_raw        small model, no checks
  small_checks     small model + checks + retries
  small_escalate   small model + checks + retries + escalation
  large_per_doc    large model per document, no checks
  large_all        large model given all documents at once, asked for the spend summary directly
Writes out/results.csv and out/accuracy_vs_cost.html (one point per config).
"""
