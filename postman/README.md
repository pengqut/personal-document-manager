# PDM Postman Testing

The collection follows Tutorial 10 Part 1. Normal POST request data is sent using **Body -> raw -> JSON** with `Content-Type: application/json`.

GET requests have no request body. File-upload requests remain **form-data** because `/api/upload` sends an actual file and the application reads `request.files`. This is the only body-format exception.

Use the `PDM Local Test` environment and run requests in the planned requirement order.
