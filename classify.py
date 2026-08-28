EXTENSION_MAP = {
    'document': [
        'pdf', 'doc', 'docx', 'txt', 'md', 'rtf', 'odt',
        'xls', 'xlsx', 'ods', 'csv', 'tsv',
        'ppt', 'pptx', 'odp', 'epub'
    ],
    'image': [
        'jpg', 'jpeg', 'png', 'gif', 'bmp', 'webp', 'svg',
        'tiff', 'tif', 'heic', 'heif', 'ico', 'psd', 'raw'
    ],
    'audio': [
        'mp3', 'wav', 'aac', 'flac', 'ogg', 'm4a', 'wma',
        'aiff', 'opus', 'mid', 'midi'
    ],
    'video': [
        'mp4', 'avi', 'mkv', 'mov', 'wmv', 'webm', 'flv',
        'm4v', 'mpeg', 'mpg', '3gp'
    ]
}


# Work out a file's category
def classify(filename):
    if not filename or '.' not in filename:
        return 'other'
    extension = filename.rsplit('.', 1)[-1].lower()
    for category, extensions in EXTENSION_MAP.items():
        if extension in extensions:
            return category
    return 'other'
