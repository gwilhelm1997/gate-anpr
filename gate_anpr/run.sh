#!/command/with-contenv bashio
# shellcheck shell=bash
bashio::log.info "Starting Gate ANPR ${GATE_ANPR_VERSION}"
exec /opt/venv/bin/python -m gate_anpr
