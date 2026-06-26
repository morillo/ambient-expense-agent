import json

import yaml

# Load traces
with open("artifacts/traces/generated_traces.json", encoding="utf-8") as f:
    traces = json.load(f)

# Load config
with open("tests/eval/eval_config.yaml", encoding="utf-8") as f:
    config = yaml.safe_load(f)

custom_metrics = {m["name"]: m["custom_function"] for m in config["custom_metrics"]}

for case in traces["eval_cases"]:
    print(f"\nEvaluating case: {case['eval_case_id']}")
    for metric_name, func_str in custom_metrics.items():
        print(f"  Metric: {metric_name}")
        # Compile and execute the custom function
        loc = {}
        try:
            exec(func_str, globals(), loc)
            evaluate_fn = loc["evaluate"]
            result = evaluate_fn(case)
            print(f"    Result: {result}")
        except Exception:
            import traceback

            traceback.print_exc()
