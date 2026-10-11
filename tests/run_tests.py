from datetime import datetime
from pathlib import Path
import subprocess
import sys

TESTS_DIR = Path(__file__).resolve().parent
RESULTS_DIR = TESTS_DIR / "test_results"

RESULTS_DIR.mkdir(exist_ok=True)

test_files = sorted(TESTS_DIR.glob("test_*.py"))

timestamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
result_file = RESULTS_DIR / f"test_results_{timestamp}.txt"

result = subprocess.run(
    [
        sys.executable,
        "-m",
        "pytest",
        *[str(file) for file in test_files],
        "-v"
    ],
    cwd=TESTS_DIR.parent,
    capture_output=True,
    text=True
)

output = result.stdout + result.stderr

print(output)

overall = "PASSED" if result.returncode == 0 else "FAILED"

summary = (
    "\n" + "=" * 60 +
    f"\nOverall result: {overall}"
    f"\nTest record: {result_file}"
    "\n" + "=" * 60
)

print(summary)

with result_file.open("w", encoding="utf-8") as file:
    file.write(output)
    file.write(summary)

sys.exit(result.returncode)