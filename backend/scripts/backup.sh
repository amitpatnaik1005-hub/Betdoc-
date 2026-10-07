#!/bin/bash
set -euo pipefail

# Law 7: Timezone Rigor
BACKUP_DATE=$(date -u +"%Y%m%dT%H%M%SZ")
BACKUP_DIR="/backups"
BACKUP_FILE="${BACKUP_DIR}/betdoc_prod_${BACKUP_DATE}.sql.gz"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Starting database backup..."

# Ensure the backup directory exists
mkdir -p "${BACKUP_DIR}"

# Run pg_dump and pipe directly to gzip
# Using PGPASSWORD ensures we don't prompt for password
PGPASSWORD="${POSTGRES_PASSWORD}" pg_dump -h "${POSTGRES_HOST}" -U "${POSTGRES_USER}" -d "${POSTGRES_DB}" | gzip > "${BACKUP_FILE}"

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Backup completed: ${BACKUP_FILE}"

# Keep only the last 7 days of backups
find "${BACKUP_DIR}" -type f -name "*.sql.gz" -mtime +7 -delete

echo "[$(date -u +"%Y-%m-%dT%H:%M:%SZ")] Cleanup of old backups completed."
