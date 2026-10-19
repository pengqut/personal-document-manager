import subprocess
import sys


# Run the complete unit-test suite together.
result = subprocess.run([
    sys.executable,
    '-m',
    'pytest',
    'tests',
    '-v'
])

sys.exit(result.returncode)