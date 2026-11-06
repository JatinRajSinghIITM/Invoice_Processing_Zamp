"""
Quick CLI harness to validate the pipeline end-to-end before building the UI.
Run: python run_cli_test.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from core import database
from core.pipeline import run_pipeline

SAMPLES = Path(__file__).parent / "sample_invoices"


def run_one(pdf_path):
    print(f"\n{'='*70}\n{pdf_path.name}\n{'='*70}")
    result = None
    for item in run_pipeline(str(pdf_path), pdf_path.name):
        if hasattr(item, "stage"):  # StageResult
            icon = {"ok": "[OK]", "warning": "[!!]", "fail": "[XX]"}[item.status]
            print(f"  {icon} {item.stage}: {item.summary}")
            if item.detail:
                print(f"        {item.detail}")
        else:
            result = item
    print(f"  >>> DECISION: {result.decision.status}")
    return result


if __name__ == "__main__":
    database.reset_db()

    files = sorted(SAMPLES.glob("*.pdf"))
    results = {}
    for f in files:
        results[f.name] = run_one(f)
        if "02_edge_duplicate" in f.name:
            # immediately resubmit to demonstrate duplicate detection
            print("\n  --- resubmitting same invoice to test duplicate detection ---")
            results[f.name + " (resubmit)"] = run_one(f)

    print(f"\n{'='*70}\nSUMMARY\n{'='*70}")
    for name, r in results.items():
        print(f"{name:55s} -> {r.decision.status}")
