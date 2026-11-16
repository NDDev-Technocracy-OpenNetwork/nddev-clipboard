set shell := ["bash", "-euo", "pipefail", "-c"]

# Lists the supported commands.
default:
    @just --list

# Fetches locked source and test dependencies without building the product.
setup:
    python scripts/ci.py setup

# Fetches only the declared daemon dependencies for the minimum-compiler check.
[group("ci")]
setup-daemon:
    cargo fetch --locked --manifest-path daemon/Cargo.toml

# Prepares GNOME prerequisites only in an ephemeral public Linux CI job.
[group("ci")]
setup-platform:
    python scripts/ci.py setup-platform

# Checks recipes, source formats, shell safety and cross-language invariants.
check:
    just --fmt --check
    python scripts/ci.py check

# Runs both feature sets and the platform's existing native and synthetic tests.
test:
    python scripts/ci.py test

# Packages and verifies the same binaries that the native tests exercised.
build: test
    python scripts/ci.py build

# Runs the complete source and current-platform qualification.
verify: check secrets build

# Reuses the separate source check while qualifying one native CI platform.
[group("ci")]
verify-native: build

# Runs the GNOME source, logic and framing checks on the declared OS tuple.
[group("ci")]
test-extension:
    python scripts/ci.py test-extension

# Prints the exact SDK identity for the daemon's declared minimum Rust version.
[group("ci")]
minimum-rust-version:
    @python scripts/ci.py minimum-rust

# Checks the same manifest with its declared minimum compiler.
[group("ci")]
check-minimum-rust:
    python scripts/ci.py check-minimum-rust

# Retains the accepted-source full-history secret check.
[group("security")]
secrets:
    betterleaks git --no-banner --redact --log-opts="--all" .
