# Zagtag Extension SDK

Python SDK for authoring Zagtag extensions using the loopback HTTP protocol.

## Install with uv

Pin to a released version (immutable git tag):

```sh
uv add "zagtag-extension-sdk @ git+https://github.com/reaganiwadha/zagtag-python-sdk.git@sdk-v0.2.0"
```

Or track the latest unreleased changes:

```sh
uv add "zagtag-extension-sdk @ git+https://github.com/reaganiwadha/zagtag-python-sdk.git@main"
```

`uv` builds the wheel locally from source, so there is nothing to download
beyond the git checkout.

## Releasing

The SDK source lives in the Zagtag monorepo (`zagtag-rs/sdk-python`); the public
[`zagtag-python-sdk`](https://github.com/reaganiwadha/zagtag-python-sdk) repo is a
one-way mirror used purely for distribution.

To cut a release: update `version` in `pyproject.toml`, then push a matching tag
such as `sdk-v0.1.0`. GitHub Actions build-checks the package, mirrors the source
to the public repo's `main`, and re-points the tag there so testers can pin to it.
