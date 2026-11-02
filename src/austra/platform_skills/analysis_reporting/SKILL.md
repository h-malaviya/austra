# Verified analysis and visual reporting

Use this method for questions that compare saved quantities, calculate requirements,
or ask for a chart. It is platform guidance; it supplies no business formula or rule.

1. Identify every relevant active schema and read the complete current record set.
   Verify distinct record IDs against each schema's count. Treat a truncated result,
   repeated page, or conflicting duplicate as an unresolved input, not a zero.
2. Separate production or operational records from demo records. Never add demo
   copies to verified inventory. Check status, reservations, QC holds, units, dates,
   and product compatibility before adding quantities.
3. Use `calculate_decimal` or `calculate_expression` for arithmetic that is not
   supplied by a deterministic report. Only use a reviewed formula with sourced
   numeric inputs; a plain-language formula must be transcribed and checked before
   evaluation. Quote the exact source values and active approved multiplier. Reconcile
   subtotals with their rows and avoid independently calculating figures in prose.
4. An order-wise plan must allocate shared finished stock only once. State the
   allocation assumption. For each order, show demand, eligible finished stock,
   production gap, and each raw-material requirement. Show current raw inventory
   and material gaps separately. Mark missing or incompatible inputs as unknown.
5. Give one complete answer. Put the verdict first, then a visual or structured
   view containing order-wise and material-wise detail. Use A2UI where available;
   keep a complete textual fallback for other channels. Do not promise another
   reply after ending the current one.
6. Calculations do not authorize stock reservation, purchasing, production,
   dispatch, or changes to a saved business rule. Proposed reusable code remains
   a reviewed source artifact; never run model-authored code in the service.
7. For a reusable business calculation outside a built-in report, propose a
   `formula` draft. Its JSON contains an `expression` and `inputs`; each input
   names an approved record `schema`, an identity `match_field`, and a numeric
   `value_field`. An administrator must approve the draft before
   `run_approved_formula` can use it. That tool accepts one record identity and
   optional scenario overrides, then returns the formula version, source record
   IDs, source values, overrides, and exact decimal result. Label scenarios as
   hypothetical. Missing or conflicting sources require correction, not a guess.
