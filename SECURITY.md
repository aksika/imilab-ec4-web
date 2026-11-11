# Security

This project is a local helper for cameras on your own network.  It does not
ship with credentials and does not need a cloud account password in the
repository.

## Never commit

* `~/.ec4/config.json`
* `~/.ec4/imi_tokens.json`
* `~/.ec4/iot_creds.json`
* `~/.ec4/camera_hosts.json`
* browser cookies, session dumps, packet captures, or screenshots of the app

The repository `.gitignore` excludes local config, caches, logs, captures, and
media.  The helper stores runtime state outside the repository in `~/.ec4/`
with mode `0600`; the password is in the macOS Keychain.

## Reporting

If you find a security issue in this code, open a private security advisory on
GitHub rather than posting tokens, camera IDs, or logs in a public issue.
