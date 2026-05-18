FROM python:3.12-slim-bookworm

ARG PACKAGE_VERSION=1.0.5
ARG USER=repeater
ARG GROUP=repeater
ARG PUID=15888
ARG PGID=15888

ENV INSTALL_DIR=/opt/pymc_repeater \
    CONFIG_DIR=/etc/pymc_repeater \
    DATA_DIR=/var/lib/pymc_repeater \
    HOME_DIR=/home/${USER} \
    PATH=/home/${USER}/.local/bin:${PATH} \
    PYTHONUNBUFFERED=1 \
    SETUPTOOLS_SCM_PRETEND_VERSION_FOR_PYMC_REPEATER=${PACKAGE_VERSION} \
    PUID=${PUID} \
    PGID=${PGID}

# Install runtime dependencies only
RUN DEBIAN_FRONTEND=noninteractive apt-get update && apt-get install -y \
    libffi-dev \
    python3-rrdtool \
    jq \
    wget \
    libusb-1.0-0 \
    swig \
    git \
    build-essential \
    python3-dev \
    && rm -rf /var/lib/apt/lists/*

# Create the group and user in order to run without root privileges
RUN groupadd --gid "$PGID" "$GROUP" \
    && useradd --uid "$PUID" --gid "$PGID" --home-dir "$HOME_DIR" --create-home --shell /usr/bin/bash "$USER"

# Create runtime directories
RUN mkdir -p ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} \
    && chown -R "$USER":"$GROUP" ${INSTALL_DIR} ${CONFIG_DIR} ${DATA_DIR} ${HOME_DIR}

WORKDIR ${INSTALL_DIR}

# Copy source
COPY repeater ./repeater
COPY pyproject.toml .
COPY config.yaml.example .
COPY radio-presets.json .
COPY radio-settings.json .
COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh

# Switch to the unprivileged runtime user
USER ${USER}

# Install package
RUN pip install --no-cache-dir .

USER root

RUN chmod +x /usr/local/bin/docker-entrypoint.sh

USER ${USER}

EXPOSE 8000

ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
