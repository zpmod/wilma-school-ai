#!/usr/bin/env python3
"""Pre-commit PII scanner.

Scans staged files for common PII patterns that should never appear
in the public repository. Run via pre-commit hook or CI.

Exit code 0 = clean, 1 = PII detected.
"""

import re
import subprocess
import sys

# Patterns that should NEVER appear in committed code
PII_PATTERNS = [
    # Finnish SSN (DDMMYY-XXXX or DDMMYY+XXXX or DDMMYYAXXXX)
    (r"\b\d{6}[-+A]\d{3}[A-Z0-9]\b", "Finnish SSN"),
    # Email addresses (except generic examples)
    (r"\b[a-zA-Z0-9._%+-]+@(?!example\.com|test\.com|school\.example\.fi)[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}\b", "Email address"),
    # Finnish phone numbers
    (r"\b(?:\+358|0)\d{8,10}\b", "Finnish phone number"),
    # Private IP ranges that indicate our infra (be specific)
    (r"192\.168\.0\.\d{1,3}", "Private IP (192.168.0.x)"),
    (r"192\.168\.1\.\d{1,3}", "Private IP (192.168.1.x)"),
    # MAC addresses
    (r"\b([0-9A-Fa-f]{2}:){5}[0-9A-Fa-f]{2}\b", "MAC address"),
    # Specific school hostnames (real schools)
    (r"helsinki\.inschool\.fi", "Real school hostname"),
    # SSH key paths (add your own private key filenames here)
    # (r"your_private_key_name", "Private SSH key reference"),
    # HA long-lived tokens (JWT format)
    (r"eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}", "JWT token"),
]

# Files to skip (binary, generated, etc.)
SKIP_EXTENSIONS = {".db", ".pyc", ".whl", ".png", ".jpg", ".gif", ".ico"}


def get_staged_files() -> list[str]:
    """Get list of staged files from git."""
    result = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        capture_output=True, text=True
    )
    return [f for f in result.stdout.strip().split("\n") if f]


def scan_file(filepath: str) -> list[tuple[int, str, str]]:
    """Scan a single file for PII patterns. Returns list of (line_no, pattern_name, match)."""
    findings = []
    try:
        with open(filepath, "r", encoding="utf-8", errors="ignore") as f:
            for line_no, line in enumerate(f, 1):
                for pattern, name in PII_PATTERNS:
                    matches = re.findall(pattern, line)
                    for match in matches:
                        match_str = match if isinstance(match, str) else ":".join(match)
                        findings.append((line_no, name, match_str))
    except (OSError, UnicodeDecodeError):
        pass
    return findings


def main() -> int:
    files = get_staged_files()
    if not files:
        return 0

    all_findings: dict[str, list] = {}
    for filepath in files:
        if any(filepath.endswith(ext) for ext in SKIP_EXTENSIONS):
            continue
        findings = scan_file(filepath)
        if findings:
            all_findings[filepath] = findings

    if not all_findings:
        print("✓ No PII patterns detected.")
        return 0

    print("⚠️  PII DETECTED — commit blocked!\n")
    for filepath, findings in all_findings.items():
        print(f"  {filepath}:")
        for line_no, name, match in findings:
            print(f"    L{line_no}: [{name}] {match[:40]}...")
    print(f"\n  Total: {sum(len(f) for f in all_findings.values())} findings in {len(all_findings)} files.")
    print("  Fix these before committing to the public repo.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
