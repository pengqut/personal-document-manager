# API Testing

## Prerequisites

- Run API tests only in DEV/TEST with a separate database. Never target the deployed application or its database: tests change administrator setup, users, sessions, quotas and files.

- Run FR-01 and FR-02 as a separate test sequence, starting with an empty DEV/TEST database. Run FR-01 first, then FR-02 using the same database. These tests initialize the administrator and create user/session data, which can affect the starting conditions of other tests.
