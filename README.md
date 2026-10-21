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

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
unset DATABASE_URL
unset PDM_DB_PATH
export PDM_PORT=8080
python app.py
```

## How to run (PROD)

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
export DATABASE_URL='postgresql://DB_USER:DB_PASSWORD@DB_HOST:5432/pdm_prod'
export PDM_PORT=8080
python app.py
```

## PostgreSQL Setup (Ubuntu EC2)

Replace `DB_PRIVATE_IP`, `APP1_PRIVATE_IP`, `APP2_PRIVATE_IP` and `DB_PASSWORD` with your actual values. Keep all three EC2 instances in the same VPC.

1. **Install and start PostgreSQL — database EC2**

   ```bash
   sudo apt update
   sudo apt install -y postgresql
   sudo systemctl enable --now postgresql
   ```

2. **Create the application account — database EC2**

   ```bash
   sudo -u postgres psql
   ```

   Run inside psql; enter the password when prompted:

   ```sql
   SET password_encryption = 'scram-sha-256';
   CREATE ROLE pdm_user LOGIN CREATEDB;
   GRANT CONNECT ON DATABASE postgres TO pdm_user;
   \password pdm_user
   SHOW config_file;
   SHOW hba_file;
   \q
   ```

3. **Allow remote connections — postgresql.conf**

   Open the `config_file` path from step 2 with `sudo nano`. Set:

   ```ini
   listen_addresses = 'localhost,DB_PRIVATE_IP'
   port = 5432
   ```

4. **Allow both application instances — pg_hba.conf**

   Open the `hba_file` path from step 2 with `sudo nano`. Add these lines before any broader matching host rules, then restart PostgreSQL:

   ```text
   host pdm_prod,postgres pdm_user APP1_PRIVATE_IP/32 scram-sha-256
   host pdm_prod,postgres pdm_user APP2_PRIVATE_IP/32 scram-sha-256
   ```

   ```bash
   sudo systemctl restart postgresql
   ```

5. **Allow database traffic — AWS security groups**

   On the database EC2 security group, add an inbound rule: **PostgreSQL / TCP 5432 / Source: application EC2 security group**. Ensure both application instances use that group and can send outbound traffic to the database on port 5432.

6. **Start both applications — application EC2 instances**

   Use the same database URL on both instances and follow **How to run (PROD)**:

   ```bash
   export DATABASE_URL='postgresql://pdm_user:DB_PASSWORD@DB_PRIVATE_IP:5432/pdm_prod'
   ```

   URL-encode special characters in the password. Start the first instance and wait for initialization to finish before starting the second. The application creates `pdm_prod` and its tables automatically. Verify upload/download through the ALB address.

## Administrator Setup and Login

1. **Initialize:** open [http://localhost:8080](http://localhost:8080). Until an administrator has a saved password, application pages redirect to `/admin-setup.html`. Enter the administrator username and password.
2. **Administrator login:** manually open [http://localhost:8080/admin-login.html](http://localhost:8080/admin-login.html) and use the administrator credentials. Successful login opens `/admin.html`.
3. **Ordinary user login:** open [http://localhost:8080/login.html](http://localhost:8080/login.html). This entry accepts ordinary users only.

### Reset Administrator Setup

1. **Stop the application** 

2. **Connect to the application database:**

3. **Clear the administrator password and sessions** 

   ```sql
   BEGIN;
   DELETE FROM sessions WHERE user_id IN (SELECT id FROM users WHERE role = 'admin');
   UPDATE users SET password_hash = '' WHERE role = 'admin';
   COMMIT;
   ```

4. **Restart and open the application.**

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
python tests/run_test.py
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
