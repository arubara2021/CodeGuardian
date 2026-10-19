# sandtest — deep integration test

Runs every CodeGuardian phase that exists against a real GitHub clone
(`pallets/click`) and a synthetic 8-language repository. Reports pass,
fail, skip, xfail, and warning per section. Does not stop at the first
failure.

## Run

    source .venv/bin/activate
    export GROQ_API_KEY="gsk_..."
    python sandtest/deep_test.py

## Requires

- `bwrap` on PATH
- `~/.cache/codeguardian-target/release/codeguardian-rpcd`
- `python/codeguardian/_native.so`
- `GROQ_API_KEY` in environment

## What lives in work/

`work/` is recreated every run, fully disposable, gitignored.
