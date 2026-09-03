#!/usr/bin/env bash
# Bootstrap the local MariaDB/MySQL instance for AssetVault.
# Creates the itam_db database and the itam_user account used by backend/.env.
set -e

mysql -u root <<'SQL'
CREATE DATABASE IF NOT EXISTS itam_db CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;
CREATE USER IF NOT EXISTS 'itam_user'@'%' IDENTIFIED BY 'itam_pass_2026';
CREATE USER IF NOT EXISTS 'itam_user'@'localhost' IDENTIFIED BY 'itam_pass_2026';
GRANT ALL PRIVILEGES ON itam_db.* TO 'itam_user'@'%';
GRANT ALL PRIVILEGES ON itam_db.* TO 'itam_user'@'localhost';
FLUSH PRIVILEGES;
SQL

echo "MySQL bootstrap complete: itam_db ready."
