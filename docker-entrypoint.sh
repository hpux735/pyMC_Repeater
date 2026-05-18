#!/bin/sh
set -eu

INSTALL_DIR="${INSTALL_DIR:-/opt/pymc_repeater}"
CONFIG_DIR="${CONFIG_DIR:-/etc/pymc_repeater}"
CONFIG_PATH="${PYMC_REPEATER_CONFIG:-${CONFIG_DIR}/config.yaml}"
EXAMPLE_PATH="${CONFIG_DIR}/config.yaml.example"
BUNDLED_EXAMPLE_PATH="${INSTALL_DIR}/config.yaml.example"
RUNTIME_USER="${USER:-repeater}"
RUNTIME_UID="${PUID:-unknown}"
RUNTIME_GID="${PGID:-unknown}"

mkdir -p "${CONFIG_DIR}"

copy_or_die() {
    src="$1"
    dest="$2"
    if ! cp "${src}" "${dest}"; then
        echo "Failed to initialize ${dest} from ${src}." >&2
        echo "If you are bind-mounting ./config.yaml, ensure the host path is writable by ${RUNTIME_USER} (${RUNTIME_UID}:${RUNTIME_GID})." >&2
        exit 1
    fi
}

if [ ! -f "${EXAMPLE_PATH}" ] && [ -f "${BUNDLED_EXAMPLE_PATH}" ]; then
    copy_or_die "${BUNDLED_EXAMPLE_PATH}" "${EXAMPLE_PATH}"
fi

if [ -d "${CONFIG_PATH}" ]; then
    if [ ! -s "${CONFIG_PATH}/config.yaml" ] && [ -f "${EXAMPLE_PATH}" ]; then
        copy_or_die "${EXAMPLE_PATH}" "${CONFIG_PATH}/config.yaml"
    fi
    CONFIG_PATH="${CONFIG_PATH}/config.yaml"
elif [ ! -s "${CONFIG_PATH}" ] && [ -f "${EXAMPLE_PATH}" ]; then
    copy_or_die "${EXAMPLE_PATH}" "${CONFIG_PATH}"
fi

exec python3 -m repeater.main --config "${CONFIG_PATH}"
