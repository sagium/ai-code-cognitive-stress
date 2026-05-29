#!/usr/bin/env bash
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SKILLS_DIR="${HOME}/.claude/skills"
TARGET="${SKILLS_DIR}/ai-code-cognitive-stress"

mkdir -p "${SKILLS_DIR}"

if [ -e "${TARGET}" ] || [ -L "${TARGET}" ]; then
    existing="$(readlink -f "${TARGET}" 2>/dev/null || echo "${TARGET}")"
    if [ "${existing}" = "${REPO_DIR}" ]; then
        echo "Already installed: ${TARGET} → ${REPO_DIR}"
        exit 0
    fi
    echo "Path already exists at ${TARGET} (points to ${existing})."
    echo "Remove it first if you want to reinstall:"
    echo "    rm '${TARGET}' && ${BASH_SOURCE[0]}"
    exit 1
fi

ln -s "${REPO_DIR}" "${TARGET}"
echo "Installed: ${TARGET} → ${REPO_DIR}"

if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "WARNING: python3 not found on PATH. The skill needs Python 3.10+."
    exit 0
fi

py_version="$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')"
echo "Python: ${py_version}"

if ! command -v glab >/dev/null 2>&1; then
    echo "Note: glab not installed — GitLab interruption events will be unavailable."
fi

echo
echo "Restart Claude Code to pick up the new skill."
