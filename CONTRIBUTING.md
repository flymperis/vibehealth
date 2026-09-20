# Contributing

Thanks for helping. VibeHealth is a small project; keep changes focused and explain the why.

## Run the tests from source

```sh
cd backend
python -m venv .venv && . .venv/bin/activate     # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt
pytest -q
```

The tests use synthetic data, a temporary data folder and fake Paperless and Ollama servers: they need no network and no
running services. `sh backend/verify-pinned.sh` runs them against the pinned versions in a throw-away virtualenv.

Frontend:

```sh
cd frontend
npm ci
npx tsc --noEmit && npm run build
npm run dev          # proxies /api to http://127.0.0.1:5001 (VITE_API_TARGET changes it)
```

## Code style

- Python 3.12, type hints, small functions, comments that explain why. Match the surrounding code.
- TypeScript strict mode; user-visible text goes through `frontend/src/i18n.tsx` in **both** English and Greek.
- Add or update a test for every behaviour change, especially anything touching authentication, uploads, the sandbox or
  the verification rule. Do not weaken a security check to make a test pass.
- Use documentation addresses in examples and tests (`192.0.2.x`, `198.51.100.x`, `example.com`), never real ones.

## No private data, ever

Do not put real medical documents, lab values, names, tokens, passwords, addresses of your own machines or logs from your
own instance in issues, pull requests, tests or fixtures. Use synthetic data. If a bug needs a sample page, describe it or
make a fake one.

Report security problems privately: see [SECURITY.md](SECURITY.md).

## Licence

By contributing you agree that your contribution is licensed under the [AGPL-3.0](LICENSE).
