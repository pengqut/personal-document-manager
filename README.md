# Personal Document Manager

This is a demo project for Assignment 1 of IFN636.
The system helps users organize uploaded files into different categories.

## Architecture

Built with a Bottle server that serves static pages and communicates via a JSON-based API with an SQLite database.

- `app.py` — routes, service logic and the API
- `validate.py` — username and password rules, password hashing
- `limits.py` — file size and file count checks
- `classify.py` — maps a file extension to a category
- `database.py` — SQLite schema and connection
- `static/` — web files
- `config.json` - system configuration file
- `pdm.db` - database file

User uploaded files go into `uploads/` under a random name, so two files with the same name cannot overwrite each other.

## How to run (DEV)

- `pip install -r requirements.txt`
- `python3 app.py`
- Open `http://localhost:80/login.html`

## Testing

- `python3 -m unittest discover -v`

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
