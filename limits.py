# Check if a file is over the size limit
def is_file_too_big(file_size_bytes, max_file_mb):
    return file_size_bytes > max_file_mb * 1024 * 1024


# Check if a user has reached their file quota
def is_quota_reached(files_used, file_quota):
    return files_used >= file_quota
