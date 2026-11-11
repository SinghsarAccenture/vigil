# legacy/

Archived material from the pre-Vigil prototype. Nothing here is executable or imported.

- `placesToScrape.txt` — original list of regulators; superseded by `config/sources.yaml`.
- `scraper.cpython-314.pyc.disabled` — compiled leftover of the deleted `scraper.py`. The extension is
  renamed so Python will not load it. It read the API key from the environment (no hardcoded key), but
  disabled TLS verification and hardcoded a personal e-mail in its User-Agent.
