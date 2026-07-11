# Market workflow output contract

Write the complete Chinese market report first. Include the dated tables, macro interpretation, stock sections, risks, direct links, and limitations in the response itself.

On the final line emit exactly one single-line JSON marker:

```text
OPENCODE_WORKFLOW_RESULT_JSON: {"status":"completed","summary":"coverage and audit summary","artifacts":[],"quality_gate_passed":true,"target_date":"YYYY-MM-DD","timezone":"Asia/Singapore","markets":["US","Japan","Korea","Hong Kong"],"stock_count":16}
```

- Use `completed` only after the evidence audit passes; otherwise use `failed` and set `quality_gate_passed` to `false`.
- `artifacts` must remain empty. The generic runner owns response and manifest paths.
- The generic runner computes authoritative HTTP source URLs and counts from the final response. Do not ask the model to count them in the marker.
- `stock_count` is the number of distinct individual stocks analyzed, excluding indices, ETFs, futures, FX, and cryptocurrencies.
- Emit no text after the marker.
