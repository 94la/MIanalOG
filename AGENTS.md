# MIanalOG

BTCUSDT Spot, public market data only; no API keys or trading.
Read README.md before changes. Do not run a second archive writer.
Run `.venv/bin/python -m unittest discover -s tests -v`, compileall and `node tests/chart.mjs`.
Use TemporaryDirectory in tests; never delete production data/.
Web only reads archive.sqlite and listens on loopback. Preserve existing reverse proxy routes.
Unknown depth after gaps must remain unknown; filter after price aggregation.
