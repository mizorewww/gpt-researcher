from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Any


HTTP_URL = re.compile(r"https?://[^\s<>\[\]()|]+", re.IGNORECASE)
REQUIRED_COVERAGE = {
    "S&P 500": ("s&p 500", "标普500", "标普 500", "^gspc"),
    "Dow": ("dow", "道琼斯", "^dji"),
    "Nasdaq Composite": ("nasdaq composite", "纳斯达克综合", "^ixic"),
    "Russell 2000": ("russell 2000", "罗素2000", "^rut"),
    "Nikkei 225": ("nikkei 225", "日经225", "^n225"),
    "TOPIX": ("topix", "东证", "998405.t"),
    "KOSPI": ("kospi", "^ks11"),
    "KOSDAQ": ("kosdaq", "^kq11"),
    "Hang Seng": ("hang seng", "恒生指数", "^hsi"),
    "Hang Seng TECH": ("hang seng tech", "恒生科技", "hstech"),
    "WTI": ("wti", "cl=f", "西德州"),
    "Brent": ("brent", "bz=f", "布伦特"),
    "Gold": ("gold", "gc=f", "黄金"),
    "Copper": ("copper", "hg=f", "铜"),
}
REQUIRED_MARKETS = {"US", "Japan", "Korea", "Hong Kong"}


def load_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected JSON object: {path}")
    return value


def unique_urls(text: str) -> set[str]:
    return {
        match.group(0).rstrip(".,;:!?\"'")
        for match in HTTP_URL.finditer(text)
    }


def validate_session(
    session: dict[str, Any],
    expected_input: dict[str, Any],
    observed_tool_calls: list[str] | None = None,
) -> dict[str, Any]:
    response_path = Path(str(session.get("response_path", ""))).expanduser()
    if not response_path.is_file():
        raise ValueError("session response artifact is missing")
    report = response_path.read_text(encoding="utf-8", errors="replace")
    report_lower = report.lower()
    if len(report) < 5000:
        raise ValueError("market report is too short for the required coverage")

    marker = session.get("result")
    if not isinstance(marker, dict):
        raise ValueError("session result marker is missing")
    expected_marker = {
        "status": "completed",
        "quality_gate_passed": True,
        "target_date": expected_input.get("target_date"),
        "timezone": expected_input.get("timezone"),
    }
    for key, expected in expected_marker.items():
        if marker.get(key) != expected:
            raise ValueError(
                f"result marker mismatch for {key}: expected {expected!r}, got {marker.get(key)!r}"
            )
    if marker.get("artifacts") != []:
        raise ValueError("workflow agents must not invent runner artifact paths")
    markets = marker.get("markets")
    if not isinstance(markets, list) or not REQUIRED_MARKETS.issubset(markets):
        raise ValueError("result marker does not cover all four required markets")
    stock_count = marker.get("stock_count")
    if not isinstance(stock_count, int) or stock_count < 16:
        raise ValueError("result marker reports fewer than 16 stocks")

    missing = [
        label
        for label, aliases in REQUIRED_COVERAGE.items()
        if not any(alias in report_lower for alias in aliases)
    ]
    if missing:
        raise ValueError(f"market report is missing required coverage: {missing}")

    recorded_urls = session.get("http_sources")
    urls = (
        {item for item in recorded_urls if isinstance(item, str)}
        if isinstance(recorded_urls, list)
        else unique_urls(report)
    )
    if len(urls) < 25:
        raise ValueError(
            f"market report has {len(urls)} unique HTTP sources; at least 25 are required"
        )
    tool_calls = observed_tool_calls or session.get("tool_calls")
    if not isinstance(tool_calls, list):
        raise ValueError("runner did not record session tool calls")
    evidence_classes = {
        "structured_market_data": any(
            isinstance(name, str) and name.startswith("yfinance_")
            for name in tool_calls
        ),
        "independent_web_evidence": any(
            isinstance(name, str) and name.startswith("tavily_")
            for name in tool_calls
        ),
    }
    if not all(evidence_classes.values()):
        raise ValueError(
            f"primary session did not use both configured evidence classes: {evidence_classes}"
        )
    return {
        "source_count": len(urls),
        "marker_source_count": marker.get("source_count"),
        "stock_count": stock_count,
        "evidence_classes": evidence_classes,
    }


def observed_tools_from_runtime(manifest: dict[str, Any]) -> list[str] | None:
    paths = manifest.get("paths")
    if not isinstance(paths, dict) or not isinstance(paths.get("runtime"), str):
        return None
    log_path = (
        Path(paths["runtime"]) / "xdg" / "data" / "opencode" / "log" / "opencode.log"
    )
    if not log_path.is_file():
        return None
    observed: set[str] = set()
    with log_path.open("r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = re.search(r"\bevaluated permission=([^\s]+)", line)
            if match:
                observed.add(match.group(1))
    return sorted(observed) or None


def main() -> int:
    manifest = load_json(Path(os.environ["RESEARCH_WORKFLOW_MANIFEST"]))
    sessions = manifest.get("sessions")
    inputs = manifest.get("inputs")
    if not isinstance(sessions, list) or not isinstance(inputs, list):
        raise ValueError("runner manifest has no sessions or canonical inputs")
    if len(sessions) != len(inputs) or not sessions:
        raise ValueError("session/input cardinality mismatch")
    tool_audit = manifest.get("tool_audit")
    observed_tool_calls = (
        tool_audit.get("observed")
        if isinstance(tool_audit, dict)
        and isinstance(tool_audit.get("observed"), list)
        else None
    )
    if observed_tool_calls is None:
        observed_tool_calls = observed_tools_from_runtime(manifest)
    results = []
    for session, input_record in zip(sessions, inputs):
        if not isinstance(session, dict) or not isinstance(input_record, dict):
            raise ValueError("invalid session/input manifest entry")
        expected_input = load_json(Path(str(input_record["path"])))
        results.append(
            validate_session(session, expected_input, observed_tool_calls)
        )
    print(json.dumps({"status": "passed", "sessions": results}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
