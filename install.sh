#!/bin/sh
#
# AdGuardHub — native installer for Debian/Ubuntu with systemd.
#
#   curl -fsSL https://raw.githubusercontent.com/fgrfn/adguardhub/main/install.sh | sh
#
# You are about to run a script you have not read. If you would rather look
# first — and you would be right to — do it in two steps instead:
#
#   curl -fsSL https://raw.githubusercontent.com/fgrfn/adguardhub/main/install.sh -o install.sh
#   less install.sh
#   sudo sh install.sh
#
# This script comes from `main`, but it installs the latest *release*, not the
# current state of the branch. Handing someone an untested commit because they
# ran a one-liner would be a poor trade.
#
# Re-running it upgrades an existing installation in place. Your data directory
# and /etc/adguardhub/adguardhub.env are never touched.
#
# Environment overrides, all optional:
#
#   ADGUARDHUB_VERSION        install this release instead of the newest (e.g. v0.3.0)
#   ADGUARDHUB_PORT           port to listen on (default 80)
#   ADGUARDHUB_PREFIX         where the program goes (default /opt/adguardhub)
#   ADGUARDHUB_DATA_DIR       where the database goes (default /var/lib/adguardhub)
#   ADGUARDHUB_DOWNLOAD_BASE  where to fetch releases from, for a local mirror
#
set -eu

REPO="fgrfn/adguardhub"
SERVICE_USER="adguardhub"
PREFIX="${ADGUARDHUB_PREFIX:-/opt/adguardhub}"
DATA_DIR="${ADGUARDHUB_DATA_DIR:-/var/lib/adguardhub}"
PORT="${ADGUARDHUB_PORT:-80}"
DOWNLOAD_BASE="${ADGUARDHUB_DOWNLOAD_BASE:-https://github.com/${REPO}/releases/download}"
API_BASE="${ADGUARDHUB_API_BASE:-https://api.github.com}"
UNIT_PATH="/etc/systemd/system/adguardhub.service"
ENV_DIR="/etc/adguardhub"

WORK=""
ARCHIVE=""
cleanup() {
    [ -n "$WORK" ] && rm -rf "$WORK"
}
trap cleanup EXIT INT TERM

say() { printf '\033[32m==>\033[0m %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die() {
    printf '\033[31mAdGuardHub install failed:\033[0m %s\n' "$*" >&2
    exit 1
}

# --------------------------------------------------------------------------
# What this script is willing to run on
# --------------------------------------------------------------------------

# Named narrowly on purpose. Covering every distribution badly helps nobody;
# saying plainly that this one is not supported lets you install by hand from
# the documentation instead of debugging a script that half-worked.
require_supported_system() {
    [ "$(id -u)" -eq 0 ] || die "run this as root (try: sudo sh install.sh)"
    command -v apt-get >/dev/null 2>&1 ||
        die "this installer supports Debian and Ubuntu. For anything else, use the Docker image (see docs/install.md)."
    command -v systemctl >/dev/null 2>&1 ||
        die "systemd is required to run AdGuardHub as a service. On a system without it, use the Docker image."
    command -v curl >/dev/null 2>&1 || apt_install curl
}

apt_install() {
    say "Installing $*"
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$@" >/dev/null ||
        die "apt-get could not install: $*"
}

# Can this python3 actually build a virtual environment?
#
# Not the same question as "is the venv module importable". Debian splits the
# machinery in two: `venv` itself is in the standard library and imports fine on
# a bare python3, while `ensurepip` — which is what populates a new environment
# — ships separately in python3-venv. So `import venv` succeeded on a machine
# where `python3 -m venv` could not work, this script reported every dependency
# present, and then died several steps later with "ensurepip is not available".
can_make_venv() {
    python3 -c 'import ensurepip, venv' >/dev/null 2>&1
}

# Which package provides it depends on the release. `python3-venv` is the
# meta-package and is right almost everywhere; some systems only carry the
# versioned `python3.13-venv` that its own error message names. Try the general
# one, then the one matching the interpreter that is actually installed, and
# check after each rather than trusting either to be the answer.
install_venv_support() {
    say "Installing Python virtual environment support"
    version=$(python3 -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true)
    for package in python3-venv ${version:+python${version}-venv}; do
        DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "$package" >/dev/null 2>&1 || continue
        can_make_venv && return 0
    done
    die "python3 on this system cannot create virtual environments: 'import ensurepip' fails, and neither python3-venv nor python${version}-venv provided it. Install your distribution's venv package by hand and run this script again."
}

install_dependencies() {
    missing=""
    command -v python3 >/dev/null 2>&1 || missing="$missing python3"
    command -v tar >/dev/null 2>&1 || missing="$missing tar"
    [ -e /etc/ssl/certs/ca-certificates.crt ] || missing="$missing ca-certificates"
    # Asked before python3 may have been installed, and asked again afterwards:
    # a machine with no python3 at all obviously cannot make a venv yet, and the
    # package that fixes that is named after the version apt is about to bring.
    needs_venv=no
    can_make_venv || needs_venv=yes

    if [ -n "$missing" ] || [ "$needs_venv" = yes ]; then
        say "Refreshing the package list"
        DEBIAN_FRONTEND=noninteractive apt-get update -qq >/dev/null ||
            die "apt-get update failed"
    fi
    if [ -n "$missing" ]; then
        # shellcheck disable=SC2086
        apt_install $missing
    fi
    can_make_venv || install_venv_support
}

# --------------------------------------------------------------------------
# Which release
# --------------------------------------------------------------------------

resolve_version() {
    if [ -n "${ADGUARDHUB_VERSION:-}" ]; then
        printf '%s' "$ADGUARDHUB_VERSION"
        return
    fi
    latest=$(curl -fsSL "${API_BASE}/repos/${REPO}/releases/latest" 2>/dev/null |
        sed -n 's/.*"tag_name"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p' | head -n 1)
    [ -n "$latest" ] ||
        die "could not work out the newest release. Name one explicitly, e.g. ADGUARDHUB_VERSION=v0.3.0 sh install.sh"
    printf '%s' "$latest"
}

# Compare the archive against the SHA256SUMS the release publishes beside it.
#
# This is the one thing here that is unpacked as root, so it is worth knowing it
# arrived intact. What that buys is honest but bounded: it catches a truncated
# download, a stale mirror, and a wrong ADGUARDHUB_DOWNLOAD_BASE. It does not
# defend against a compromised GitHub, because the sums come from the same place
# as the archive — for that a signature checked against a key you already hold
# would be needed, and this project publishes no such key.
verify_release() {
    archive="$1"
    sums_url="${DOWNLOAD_BASE}/${version}/SHA256SUMS"
    if ! curl -fsSL "$sums_url" -o "$WORK/SHA256SUMS" 2>/dev/null; then
        # Releases before v0.4.4 shipped without one, and installing an older
        # version has to keep working.
        note "no SHA256SUMS published for ${version} — skipping the integrity check"
        return 0
    fi
    expected=$(sed -n "s/^\([0-9a-f]\{64\}\) [ *]${archive}\$/\1/p" "$WORK/SHA256SUMS" | head -n 1)
    [ -n "$expected" ] ||
        die "SHA256SUMS for ${version} does not mention ${archive}. Refusing to install an archive nobody vouched for."
    actual=$(sha256sum "$WORK/$archive" | cut -d' ' -f1)
    [ "$expected" = "$actual" ] ||
        die "checksum mismatch on ${archive}: expected ${expected}, got ${actual}. Nothing was installed."
    note "checksum verified"
}

download_release() {
    version="$1"
    stripped="${version#v}"
    archive="adguardhub-${stripped}.tar.gz"
    url="${DOWNLOAD_BASE}/${version}/${archive}"
    say "Downloading AdGuardHub ${version}"
    note "$url"
    curl -fsSL "$url" -o "$WORK/$archive" ||
        die "could not download $url — check that release exists and has a tarball attached."
    verify_release "$archive"
    # A 404 page saved as a file is the classic way this goes wrong quietly.
    tar -tzf "$WORK/$archive" >/dev/null 2>&1 ||
        die "the downloaded file is not a valid archive. The release may not have one attached yet."
    ARCHIVE="$WORK/$archive"
}

# --------------------------------------------------------------------------
# Installing
# --------------------------------------------------------------------------

create_user() {
    if ! id "$SERVICE_USER" >/dev/null 2>&1; then
        say "Creating the $SERVICE_USER system user"
        useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" ||
            die "could not create the $SERVICE_USER user"
    fi
}

install_files() {
    say "Installing to $PREFIX"
    mkdir -p "$WORK/unpacked"
    tar -xzf "$ARCHIVE" -C "$WORK/unpacked" --strip-components=1 ||
        die "could not unpack the release"
    for required in app static requirements.txt; do
        [ -e "$WORK/unpacked/$required" ] ||
            die "the release archive is missing '$required' — it may have been built incorrectly."
    done

    mkdir -p "$PREFIX"
    # Replaced wholesale rather than merged, so a file deleted upstream does not
    # linger and get imported. The venv is kept, because rebuilding it on every
    # upgrade means a few minutes of compiling for no reason.
    rm -rf "$PREFIX/app" "$PREFIX/static"
    cp -a "$WORK/unpacked/app" "$WORK/unpacked/static" "$PREFIX/"
    cp -a "$WORK/unpacked/requirements.txt" "$PREFIX/"
}

# What the last successful dependency install was for. Kept in $PREFIX, which an
# upgrade does not wipe — only app/ and static/ are replaced.
STAMP="$PREFIX/.dependencies-installed"

# The requirements file and the interpreter that installed against it.
#
# Both, because a distribution upgrade that moves python3 leaves a venv nothing
# can import from, and matching the file alone would skip straight past that.
dependency_fingerprint() {
    printf '%s %s\n' \
        "$(sha256sum "$PREFIX/requirements.txt" | cut -d' ' -f1)" \
        "$("$PREFIX/venv/bin/python" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
}

# Whether the venv can actually import what the hub needs to start.
#
# The stamp is a claim about the past, and a venv can be broken by something that
# never touched requirements.txt — a pip run killed halfway, a package removed by
# hand. Checking costs a fraction of a second against the minute the stamp saves,
# and re-running the installer stays the way to repair an install rather than
# something the stamp can talk you out of.
#
# greenlet is in the list on purpose: v0.4.0 shipped without it on Python 3.13
# and died on its first database query, which is exactly the shape of fault a
# skipped pip run could otherwise reintroduce silently.
dependencies_importable() {
    "$PREFIX/venv/bin/python" - >/dev/null 2>&1 <<'PYTHON'
import fastapi, greenlet, httpx, sqlalchemy, uvicorn  # noqa: F401
PYTHON
}

install_python_environment() {
    # Both, not just the interpreter. A venv whose ensurepip step failed still
    # has bin/python — the symlinks are made before pip is installed — so
    # checking the interpreter alone declared a broken environment good and then
    # died on the missing pip a line later. That is the state the previous run
    # leaves behind, which made the obvious fix-and-retry fail differently.
    if [ ! -x "$PREFIX/venv/bin/python" ] || [ ! -x "$PREFIX/venv/bin/pip" ]; then
        say "Creating the Python environment"
        # Cleared rather than reused: python3 -m venv over a half-built one
        # repairs some of it and not the rest.
        rm -rf "$PREFIX/venv"
        python3 -m venv "$PREFIX/venv" ||
            die "could not create a virtualenv in $PREFIX/venv. The error above is python3's own; if it mentions ensurepip, install your distribution's python3-venv package and run this script again."
        # A rebuilt venv has nothing in it, whatever the old stamp claimed.
        rm -f "$STAMP"
    fi

    fingerprint=$(dependency_fingerprint)
    # pip spends about a minute proving that what is installed is installed, and
    # it does that on every upgrade. Most upgrades move no dependency at all —
    # and that minute is the bulk of the window in which the hub is being
    # replaced, so it is worth not spending twice for nothing.
    if [ -f "$STAMP" ] && [ "$(cat "$STAMP")" = "$fingerprint" ] && dependencies_importable; then
        say "Python dependencies are unchanged"
        return 0
    fi

    say "Installing Python dependencies (this takes a minute)"
    "$PREFIX/venv/bin/pip" install --quiet --upgrade pip >/dev/null 2>&1 || true
    "$PREFIX/venv/bin/pip" install --quiet -r "$PREFIX/requirements.txt" ||
        die "could not install the Python dependencies"
    # Written only now. A stamp for an install that failed would tell the next
    # run there was nothing to do, turning one bad upgrade into a stuck one.
    printf '%s\n' "$fingerprint" >"$STAMP"
}

prepare_data_dir() {
    say "Preparing $DATA_DIR"
    mkdir -p "$DATA_DIR" "$ENV_DIR"
    chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
    # The database and the generated encryption key live here, and nobody else
    # on the machine has any business reading either.
    chmod 750 "$DATA_DIR"
    if [ ! -e "$ENV_DIR/adguardhub.env" ]; then
        cat >"$ENV_DIR/adguardhub.env" <<'ENVFILE'
# Settings for AdGuardHub. This file is yours — upgrades never overwrite it.
# Every ADGUARDHUB_* variable works here, one per line; docs/configuration.md lists them.
#
# The encryption key is optional: with nothing set, the hub generates one on
# first start and keeps it in its data directory. Setting it here instead keeps
# the key out of the directory it protects, which is the stronger arrangement.
# Generate one with: openssl rand -base64 48
#
# ADGUARDHUB_SECRET_KEY=...
#
# ADGUARDHUB_LOG_LEVEL=INFO
ENVFILE
        chmod 640 "$ENV_DIR/adguardhub.env"
        chown root:"$SERVICE_USER" "$ENV_DIR/adguardhub.env"
    fi
}

fill_template() {
    sed -e "s|@PREFIX@|$PREFIX|g" \
        -e "s|@DATA@|$DATA_DIR|g" \
        -e "s|@USER@|$SERVICE_USER|g" \
        -e "s|@PORT@|$PORT|g" \
        -e "s|@VERSION@|$2|g" \
        "$1"
}

# The hub asks to be upgraded by creating one empty file; these two units are
# what turns that into an upgrade. Installing them is what makes the interface's
# "update now" button work — without them the button is simply not offered, and
# an upgrade is this script, run again by hand.
install_update_units() {
    [ -e "$PREFIX/adguardhub-update.service.in" ] || return 0
    say "Installing the self-update units"
    fill_template "$PREFIX/adguardhub-update.service.in" "" \
        >/etc/systemd/system/adguardhub-update.service ||
        die "could not write /etc/systemd/system/adguardhub-update.service"
    fill_template "$PREFIX/adguardhub-update.path.in" "" \
        >/etc/systemd/system/adguardhub-update.path ||
        die "could not write /etc/systemd/system/adguardhub-update.path"
    rm -f "$PREFIX/adguardhub-update.service.in" "$PREFIX/adguardhub-update.path.in"
    chmod 750 "$PREFIX/adguardhub-update.sh"
    systemctl daemon-reload
    systemctl enable --quiet --now adguardhub-update.path 2>/dev/null ||
        note "the update watcher could not be enabled; upgrades stay a manual re-run of this script."
}

install_service() {
    version="$1"
    say "Installing the systemd service"
    fill_template "$PREFIX/adguardhub.service.in" "${version#v}" >"$UNIT_PATH" ||
        die "could not write $UNIT_PATH"
    rm -f "$PREFIX/adguardhub.service.in"
    systemctl daemon-reload
    systemctl enable --quiet adguardhub 2>/dev/null || true
    systemctl restart adguardhub || die "the service did not start. Look at: journalctl -u adguardhub -n 50"
}

# systemctl restart returns as soon as the process is up, which is well before
# the application has opened its database. A hub that dies in startup and is
# restarted every five seconds by systemd therefore looked like a clean install
# — the script printed "AdGuardHub is running" over a crash loop. It is not
# entitled to say that until something answers.
wait_for_service() {
    say "Waiting for the hub to answer"
    attempt=0
    while [ "$attempt" -lt 30 ]; do
        if curl -fsS -o /dev/null "http://127.0.0.1:${PORT}/api/health" 2>/dev/null; then
            return 0
        fi
        sleep 1
        attempt=$((attempt + 1))
    done
    printf '\n' >&2
    # The reason is almost always in the last few lines, and asking someone to
    # go and find them is asking them to guess what they are looking for.
    journalctl -u adguardhub -n 25 --no-pager >&2 2>/dev/null || true
    printf '\n' >&2
    die "the hub was installed but never answered on port ${PORT}. Its own log is above; the full one is: journalctl -u adguardhub -n 100"
}

# Whether the hub is still waiting for its admin account to be created.
#
# The hub knows, so the installer asks it rather than guessing. Guessing from
# "did $PREFIX exist before this run" is wrong in both directions: a first
# install can start from a data directory restored out of a backup, and an
# upgrade can land on a hub nobody ever set up.
needs_admin_account() {
    curl -fsS "http://127.0.0.1:${PORT}/api/auth/state" 2>/dev/null |
        grep -q '"setup_required"[[:space:]]*:[[:space:]]*true'
}

report() {
    version="$1"
    address=$(hostname -I 2>/dev/null | awk '{print $1}')
    [ -n "$address" ] || address="this-host"
    url="http://${address}/"
    [ "$PORT" = "80" ] || url="http://${address}:${PORT}/"
    printf '\n'
    say "AdGuardHub ${version} is running."
    # An upgrade ends here too, and telling someone who has run this hub for
    # months to go and create the admin account is a small lie every time.
    if needs_admin_account; then
        note "Open ${url} and create the admin account."
    else
        note "Open ${url} and sign in."
    fi
    note ""
    note "Data:     $DATA_DIR   (back this up — it holds the database and the encryption key)"
    note "Settings: $ENV_DIR/adguardhub.env"
    note "Logs:     journalctl -u adguardhub -f"
    note "Upgrade:  the 'Update this hub' button under Settings, or re-run this installer"
}

main() {
    require_supported_system
    install_dependencies
    WORK=$(mktemp -d)
    version=$(resolve_version)
    download_release "$version"
    create_user
    install_files
    # The unit template travels in the archive, so the service description
    # always matches the release that is being installed.
    [ -e "$WORK/unpacked/packaging/adguardhub.service" ] &&
        cp "$WORK/unpacked/packaging/adguardhub.service" "$PREFIX/adguardhub.service.in"
    [ -e "$PREFIX/adguardhub.service.in" ] ||
        die "the release archive is missing its systemd unit template."
    # Older releases have no self-update units; they simply do not get the button.
    for extra in adguardhub-update.service adguardhub-update.path; do
        [ -e "$WORK/unpacked/packaging/$extra" ] &&
            cp "$WORK/unpacked/packaging/$extra" "$PREFIX/$extra.in"
    done
    [ -e "$WORK/unpacked/packaging/adguardhub-update.sh" ] &&
        cp "$WORK/unpacked/packaging/adguardhub-update.sh" "$PREFIX/adguardhub-update.sh"
    install_python_environment
    prepare_data_dir
    install_service "$version"
    install_update_units
    wait_for_service
    report "$version"
}

main "$@"
