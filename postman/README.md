# PDM Postman Testing

Use the `PDM Local Test` environment and run the requirement collections in order: FR-01, FR-02, FR-03, FR-04, then FR-05/FR-06.

All ordinary POST request data uses **Body -> raw -> JSON** with `Content-Type: application/json`, matching Tutorial 10. GET requests have no request body.

The only exception is `POST /api/upload`: file-upload requests use **form-data** because the application receives an actual uploaded file through `request.files`. The included file is `fixtures/quota_sample.txt`.

Environment defaults:
- `base_url`: `http://localhost:80`
- administrator: `admin001` / `Abcdefg1`
- normal test user: `alice123` / `Abcdefg1`

FR-02 should create the normal test user before FR-03 to FR-06 are run. FR-03 stores `user_id`; later collections also refresh it themselves.

## Test Scope

The collection covers:

- FR-01 Initial Administrator Creation
- FR-02 Admin Login and Access Control
- FR-03 Normal User List
- FR-04 Enable and Disable Users
- FR-05 Individual File Quota
- FR-06 Upload Quota Enforcement
- A1 Regression APIs

## Environment

Use the `PDM Local Test` environment.

Example:

```text
base_url = http://localhost:80