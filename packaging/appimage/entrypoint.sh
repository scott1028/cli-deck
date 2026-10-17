# -I: ignore PYTHONPATH, user site-packages and the cwd, so the bundled cli_deck always loads.
{{ python-executable }} -I -m cli_deck "$@"
