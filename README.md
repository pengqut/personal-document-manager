# Personal Document Manager

This is a demo project for Assignment 1 of IFN636.
The system helps users organize uploaded files into different categories.

## Architecture

The application uses a Bottle server that serves static web pages and communicates with an SQLite database through API endpoints.

- `app.py` — application routes, service logic and API endpoints
- `validate.py` — username and password validation and password hashing
- `limits.py` — file size and file count checks
- `classify.py` — maps file extensions to categories
- `database.py` — SQLite schema and database connection
- `config.json` — system configuration
- `static/` — frontend web files
- `tests/` — software test files and consolidated test runner
- `uploads/` — uploaded files
- `pdm.db` — local database file

Uploaded files are stored in the `uploads/` directory using randomly generated names to prevent files with the same original name from overwriting each other.
## How to run (DEV)

- `pip install -r requirements.txt`
- `python3 app.py`
- Open `http://localhost:80/login.html`

## Testing

Each developer is responsible for creating and maintaining software tests for the feature they develop.

Individual test files are stored in the `tests/` directory and follow the naming format:

```text
test_*.py
```

Examples:

```text
tests/test_validate.py
tests/test_limits.py
tests/test_classify.py
```

### Run All Software Tests

A consolidated test runner is provided to run all developer test files together:

```bash
python tests/run_all_tests.py
```

The runner:

- Finds all `test_*.py` files in the `tests/` directory
- Executes the tests using PyTest
- Displays individual PASS/FAIL results
- Displays the overall test result
- Saves the complete execution record in `tests/test_results/`

Example result:

```text
============================================================
Overall result: PASSED
Test record: tests/test_results/test_results_YYYY-MM-DD_HH-MM-SS.txt
============================================================
```

### Run an Individual Test File

A specific test file can also be executed separately:

```bash
python -m pytest tests/test_validate.py -v
```

This allows each developer to test their own feature independently before running the complete project test suite.

## Testing Evidence

Testing evidence may include:

- Jira testing tasks
- Individual software test files
- Consolidated software test results
- GitHub commits and pull requests
- API testing results
- Load testing results
- Monitoring results

These outputs will be used as evidence for the Assignment 2 report and demonstration.

## Deployment

- Connect server over SSH and clone the repository
- Check out the `deployment` branch
- `sudo pip install -r requirements.txt --break-system-packages`
- `sudo python3 app.py` (root is needed for port 80)
- Open URL: http://3.106.247.182

## Known limitations

- Plain HTTP only, no HTTPS
- No fixed IP, so the address changes after a restart
- `config.json` is read at startup, so a limit change needs a restart
